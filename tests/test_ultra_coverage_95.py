import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
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
from app.core.money import validate_money, MAX_ACCOUNT_BALANCE, MAX_UNIT_PRICE
from app.core.normalizer import CargoNormalizer
from app.core.validator import CargoValidator
from app.core.skill_runner import SkillRunner
from app.core.parser import parse_single_file, process_uploaded_files
from app.core.parser.eml_parser import parse_eml, html_to_plain_text
from app.api.v1.auth import register_tenant, tenant_login, admin_login, TenantRegisterRequest, TenantLoginRequest, AdminLoginRequest
from app.api.v1.extract import extract_async_upload, extract_sync, reserve_or_raise, validate_callback_url
from app.schemas.task import ExtractSyncRequest, SkillV3InputPayload, AttachmentInput


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_extract_all_error_branches():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="ExtractErrTenant", balance=Decimal("100.00"), unit_price=Decimal("1.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        tenant_info = (tenant, api_key)

        # 1. validate_callback_url with unsafe url -> 422
        with pytest.raises(HTTPException):
            await validate_callback_url("http://127.0.0.1:8000/webhook")

        # 2. extract_async_upload: subject > 255 -> 422
        tenant = await db.get(Tenant, t_id)
        tenant_info = (tenant, api_key)
        upload_f = UploadFile(filename="test.txt", file=io.BytesIO(b"test"))
        with pytest.raises(HTTPException):
            await extract_async_upload(files=[upload_f], mail_subject="A" * 300, tenant_info=tenant_info, db=db)

        # 3. extract_async_upload: unsupported extension -> 415
        tenant = await db.get(Tenant, t_id)
        tenant_info = (tenant, api_key)
        upload_bad = UploadFile(filename="malware.exe", file=io.BytesIO(b"test"))
        with pytest.raises(HTTPException):
            await extract_async_upload(files=[upload_bad], tenant_info=tenant_info, db=db)

        # 4. extract_async_upload: exceed max files -> 413
        tenant = await db.get(Tenant, t_id)
        tenant_info = (tenant, api_key)
        many_files = [UploadFile(filename=f"doc_{i}.txt", file=io.BytesIO(b"data")) for i in range(settings.MAX_UPLOAD_FILES + 1)]
        with pytest.raises(HTTPException):
            await extract_async_upload(files=many_files, tenant_info=tenant_info, db=db)

        # 5. extract_async_upload: insufficient balance -> 402
        tenant = await db.get(Tenant, t_id)
        tenant.balance = Decimal("0.00")
        await db.commit()
        tenant = await db.get(Tenant, t_id)
        tenant_info = (tenant, api_key)
        upload_f2 = UploadFile(filename="test.txt", file=io.BytesIO(b"test"))
        with pytest.raises(HTTPException):
            await extract_async_upload(files=[upload_f2], tenant_info=tenant_info, db=db)

        # 6. extract_sync failure -> 500
        tenant.balance = Decimal("100.00")
        await db.commit()
        tenant = await db.get(Tenant, t_id)
        tenant_info = (tenant, api_key)
        with patch("app.core.skill_runner.SkillRunner.extract_draft_json", side_effect=RuntimeError("LLM Down")):
            with pytest.raises(HTTPException):
                await extract_sync(ExtractSyncRequest(mail_subject="Sync", mail_body="Body"), tenant_info=tenant_info, db=db)


@pytest.mark.asyncio
async def test_auth_and_deps_deep_branches():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        pwd = "TestPassword123!"
        hashed = hash_password(pwd)
        tenant = Tenant(id=t_id, name="AuthDeepTenant", contact_email=f"{t_id}@example.com", password_hash=hashed, balance=Decimal("100.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. Login with non-existent email -> 401
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account="non_existent_email@example.com", password="pwd"), db=db)

        # 2. Login with empty password -> 400
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account=tenant.contact_email, password=""), db=db)

        # 3. Login with wrong password -> 401
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account=tenant.contact_email, password="WrongPassword!"), db=db)

        # 4. Login with inactive tenant -> 403
        tenant.is_active = False
        await db.commit()
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account=tenant.contact_email, password=pwd), db=db)

        # 5. Admin Login with admin secret
        adm_fallback = await admin_login(AdminLoginRequest(username="admin", password=settings.ADMIN_SECRET_KEY))
        assert "admin_token" in adm_fallback["data"]

        # 6. deps.py: verify_admin_access via Bearer admin token
        req_token = MagicMock(spec=Request)
        adm_token_str = create_access_token("admin", role="admin")
        req_token.headers = {"Authorization": f"Bearer {adm_token_str}"}
        assert await verify_admin_access(request=req_token, x_admin_secret=None) is True

        # 7. deps.py: verify_admin_access via raw allowed admin key
        req_raw_adm = MagicMock(spec=Request)
        req_raw_adm.headers = {"Authorization": f"Bearer {settings.ADMIN_SECRET_KEY}"}
        assert await verify_admin_access(request=req_raw_adm, x_admin_secret=None) is True

        # 9. deps.py: get_current_tenant_and_key when tenant has no active key
        tenant.is_active = True
        api_key.is_active = False
        await db.commit()

        req_adm_virt = MagicMock(spec=Request)
        req_adm_virt.headers = {}
        t_virt, k_virt = await get_current_tenant_and_key(
            request=req_adm_virt,
            auth_cred=None,
            x_api_key=None,
            x_admin_secret=settings.ADMIN_SECRET_KEY,
            x_tenant_id=t_id,
            db=db,
        )
        assert t_virt.id == t_id
        assert "admin_virtual_key" in k_virt.id


@pytest.mark.asyncio
async def test_billing_service_remaining_branches():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="BillingRemTenant", balance=Decimal("10.00"), unit_price=Decimal("1.00"), is_active=True)
        db.add(tenant)
        await db.commit()

        # 1. check_balance_available on non-existent tenant
        is_avail, bal, up = await BillingService.check_balance_available(db, "non_existent_tenant")
        assert is_avail is False

        # 2. reserve_for_new_task when balance is insufficient
        tenant.balance = Decimal("0.50")
        tenant.unit_price = Decimal("1.00")
        await db.commit()
        assert await BillingService.reserve_for_new_task(db, t_id) is None

        # 3. deduct_for_task_success on non-existent task
        assert await BillingService.deduct_for_task_success(db, t_id, "non_existent_task") is None

        # 4. update_unit_price with zero
        with pytest.raises(ValueError):
            await BillingService.update_unit_price(db, t_id, Decimal("0.0000"))

        # 5. update_unit_price exceeding max
        with pytest.raises(ValueError):
            await BillingService.update_unit_price(db, t_id, Decimal("99999.00"))


@pytest.mark.asyncio
async def test_queue_and_storage_workers():
    manager = TaskQueueManager()
    await manager.start()

    t_id = f"tenant_{uuid.uuid4().hex[:6]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="QueueWorkerTenant", balance=Decimal("50.00"), unit_price=Decimal("1.00"), is_active=True)
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            raw_input_json=json.dumps({"mail_subject": "Queue Test", "mail_body": "POL: SHANGHAI", "attachments": []}),
        )
        db.add(tenant)
        db.add(task)
        await db.commit()

    with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = {"POL": "SHANGHAI", "POD": "ROTTERDAM", "ContainerInfo": []}
        await manager.enqueue(task_id, t_id, "secret_key")
        await asyncio.sleep(0.05)

    await manager.stop()

    # Storage retention worker test
    with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await StorageService.start_retention_pruning_worker()


def test_eml_parser_and_html_cleaning():
    # 1. html_to_plain_text
    html_raw = "<html><body><p>Hello <b>World</b></p><br/><table><tr><td>Row 1</td><td>Row 2</td></tr></table></body></html>"
    txt = html_to_plain_text(html_raw)
    assert "Hello World" in txt
    assert "Row 1" in txt

    assert html_to_plain_text("") == ""
