import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app.config import settings
from app.database import AsyncSessionLocal, init_db, get_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask, WebhookLog
from app.models.billing import BillingTransaction
from app.api.deps import get_current_tenant_and_key, verify_admin_access
from app.services.auth_service import (
    authenticate_api_key,
    create_access_token,
    generate_api_key_and_secret,
    hash_api_key,
    hash_password,
    password_needs_rehash,
    verify_access_token,
    verify_password,
)
from app.services.billing_service import BillingService
from app.services.extraction_service import ExtractionService
from app.services.queue_service import TaskQueueManager
from app.services.storage_service import StorageService
from app.services.webhook_service import is_safe_webhook_url, send_webhook_notification
from app.core.money import validate_money, MAX_ACCOUNT_BALANCE, MAX_UNIT_PRICE, MIN_UNIT_PRICE
from app.core.normalizer import CargoNormalizer
from app.core.validator import CargoValidator
from app.core.skill_runner import SkillRunner
import app.core as core_pkg
from app.api.v1.auth import register_tenant, tenant_login, admin_login, TenantRegisterRequest, TenantLoginRequest, AdminLoginRequest
from app.api.v1.extract import extract_async_upload


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


def test_core_package_lazy_attrs():
    # Test __getattr__ in app.core
    norm = core_pkg.CargoNormalizer
    assert norm is not None
    val = core_pkg.CargoValidator
    assert val is not None
    sk = core_pkg.SkillRunner
    assert sk is not None

    with pytest.raises(AttributeError):
        _ = core_pkg.NonExistentModuleAttr


def test_normalizer_all_edge_cases(tmp_path):
    # 1. Non-existent reference table fallback
    norm_none = CargoNormalizer(reference_path=str(tmp_path / "no_such_ref.json"))
    assert norm_none.reference == {}

    # 2. Corrupt reference table
    bad_ref = tmp_path / "bad.json"
    bad_ref.write_text("{corrupt", encoding="utf-8")
    norm_bad = CargoNormalizer(reference_path=str(bad_ref))
    assert norm_bad.reference == {}

    # 3. _merge_contact_continuations with EMAIL label
    txt_contact = "Contact Person\nEMAIL: ops@forwarder.com\nNext line phone +86-13800000000"
    merged = norm_none._merge_contact_continuations(txt_contact)
    assert "EMAIL:" in merged

    # 4. _split_number_unit with commas & invalid floats
    n1, u1 = norm_none._split_number_unit("12,500.50 KGS")
    assert n1 == "12500.50"
    assert u1 == "KGS"

    # 5. _normalize_container_type with candidate directly in sizes
    s, t, un = norm_none._normalize_container_type("40", "")
    assert s == "40"


@pytest.mark.asyncio
async def test_billing_service_overflow_and_edge_cases():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="OverflowTenant", balance=Decimal("100.00"), unit_price=Decimal("1.00"), reserved_balance=Decimal("0.00"), is_active=True)
        db.add(tenant)
        await db.commit()

        # 1. Recharge exceeding MAX_ACCOUNT_BALANCE -> ValueError
        with pytest.raises(ValueError):
            await BillingService.recharge_balance(db, t_id, MAX_ACCOUNT_BALANCE)

        # 2. get_all_transactions without type filter
        all_tx = await BillingService.get_all_transactions(db, limit=10, transaction_type=None)
        assert isinstance(all_tx, list)

        # 3. release_task_reservation with unreserved task
        task_unres = EmailTask(id=f"task_{uuid.uuid4().hex[:8]}", tenant_id=t_id, is_reserved=False, reserved_amount=Decimal("0.00"))
        db.add(task_unres)
        await db.commit()

        rel_res = await BillingService.release_task_reservation(db, t_id, task_unres.id)
        assert rel_res is False


@pytest.mark.asyncio
async def test_queue_service_recovery_and_startup():
    manager = TaskQueueManager()

    # Recover uncompleted tasks
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="RecoverTenant", balance=Decimal("100.00"), is_active=True)
        task = EmailTask(id=f"task_{uuid.uuid4().hex[:8]}", tenant_id=t_id, status="PENDING")
        db.add(tenant)
        db.add(task)
        await db.commit()

        await manager._recover_uncompleted_tasks()
        assert manager._queue.qsize() >= 1

    await manager.stop()


def test_storage_service_prune_with_expired_files(tmp_path):
    old_file = settings.uploads_path / "old_upload_test.txt"
    old_file.write_text("old content", encoding="utf-8")
    past_time = time.time() - (100 * 86400)
    os.utime(old_file, (past_time, past_time))

    deleted = StorageService.prune_expired_uploads(days=90)
    assert deleted >= 1
    assert not old_file.exists()


@pytest.mark.asyncio
async def test_skill_runner_fallback_model():
    runner = SkillRunner()

    # Primary model fails with 500, fallback model succeeds
    resp_primary_fail = MagicMock(status_code=500, text="Internal Server Error")
    resp_fallback_ok = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"choices": [{"message": {"content": "{\"ShipperName\": \"FALLBACK_OK\"}"}}]}),
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(settings, "LLM_MAX_RETRIES", 0), \
         patch.object(settings, "LLM_FALLBACK_MODEL", "deepseek-v4-fallback"):
        mock_post.side_effect = [resp_primary_fail, resp_fallback_ok]
        res = await runner.call_llm("test prompt")
        assert "FALLBACK_OK" in res


@pytest.mark.asyncio
async def test_auth_and_deps_remaining_paths():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        pwd = "TestPassword123!"
        tenant = Tenant(
            id=t_id,
            name="RehashTenant",
            contact_email=f"{t_id}@example.com",
            password_hash=hash_password(pwd),
            balance=Decimal("100.00"),
            is_active=True,
        )
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            key_prefix=prefix,
            key_hash=key_hash,
            api_secret=secret,
            is_active=False,  # Inactive API key
        )
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. Login with inactive API Key -> 401
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account=raw_key), db=db)

        # 2. Login with API key prefix mismatch / wrong hash -> 401
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account=f"{prefix}.wrong_secret_hash"), db=db)

        # 3. get_current_tenant_and_key with missing token/header -> 401
        req_empty = MagicMock(spec=Request)
        req_empty.headers = {}
        with pytest.raises(HTTPException):
            await get_current_tenant_and_key(
                request=req_empty,
                auth_cred=None,
                x_api_key=None,
                x_admin_secret=None,
                x_tenant_id=None,
                db=db,
            )

        # 4. verify_admin_access in DEBUG mode from localhost
        req_local = MagicMock(spec=Request)
        req_local.headers = {}
        req_local.client = MagicMock(host="127.0.0.1")
        with patch.object(settings, "DEBUG", True):
            assert await verify_admin_access(request=req_local, x_admin_secret=None) is True


@pytest.mark.asyncio
async def test_extraction_service_edge_cases():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="ExtEdgeTenant", balance=Decimal("100.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. Process task without tenant_secret lookup
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            raw_input_json=json.dumps({"mail_subject": "Lookup Secret", "mail_body": "POL: SHANGHAI", "attachments": []}),
        )
        db.add(task)
        await db.commit()

        with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"POL": "SHANGHAI", "POD": "ROTTERDAM", "ContainerInfo": []}
            await ExtractionService.process_task(task_id, tenant_secret=None)

        # 2. Process file task with invalid file_paths_str
        task_id_bad = f"task_{uuid.uuid4().hex[:8]}"
        task_bad = EmailTask(
            id=task_id_bad,
            tenant_id=t_id,
            input_type="FILE",
            status="PENDING",
            file_paths="invalid_not_a_json_list",
        )
        db.add(task_bad)
        await db.commit()

        await ExtractionService.process_task(task_id_bad, tenant_secret=secret)

        async with AsyncSessionLocal() as db2:
            t_bad = await db2.get(EmailTask, task_id_bad)
            assert t_bad.status == "FAILED"

        # 3. Process file task with missing file
        task_id_miss = f"task_{uuid.uuid4().hex[:8]}"
        task_miss = EmailTask(
            id=task_id_miss,
            tenant_id=t_id,
            input_type="FILE",
            status="PENDING",
            file_paths=json.dumps([str(settings.uploads_path / "non_existent_file_123.txt")]),
        )
        db.add(task_miss)
        await db.commit()

        await ExtractionService.process_task(task_id_miss, tenant_secret=secret)

        async with AsyncSessionLocal() as db3:
            t_miss = await db3.get(EmailTask, task_id_miss)
            assert t_miss.status == "FAILED"


@pytest.mark.asyncio
async def test_extract_async_upload_chunk_read_error():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="UploadErrTenant", balance=Decimal("100.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        tenant_info = (tenant, api_key)

        # Upload file that throws during read
        mock_file_err = MagicMock(spec=UploadFile)
        mock_file_err.filename = "corrupted.txt"
        mock_file_err.read = AsyncMock(side_effect=IOError("Disk Read Failure"))
        mock_file_err.close = AsyncMock()

        with pytest.raises(IOError):
            await extract_async_upload(files=[mock_file_err], mail_subject="Test Upload", callback_url=None, tenant_info=tenant_info, db=db)
