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
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal, init_db, get_db
from app.main import app
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask, WebhookLog
from app.models.billing import BillingTransaction
from app.api.deps import get_current_tenant_and_key
from app.api.v1.extract import extract_async_json, extract_async_upload, ExtractAsyncRequest
from app.schemas.task import AttachmentInput
from app.services.auth_service import generate_api_key_and_secret
from app.services.billing_service import BillingService
from app.services.queue_service import task_queue, TaskQueueManager
from app.core.money import MAX_ACCOUNT_BALANCE, MAX_UNIT_PRICE, MIN_UNIT_PRICE


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_active_tenant_and_key(balance=Decimal("200.00"), unit_price=Decimal("0.50")):
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name=f"PushTenant {t_id}",
            balance=balance,
            unit_price=unit_price,
            is_active=True,
        )
        api_key = ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            name="test-key",
            key_prefix=prefix,
            key_hash=key_hash,
            api_secret=secret,
            is_active=True,
        )
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # Reload with selectinload
        stmt = select(Tenant).options(selectinload(Tenant.api_keys)).where(Tenant.id == t_id)
        tenant_loaded = (await db.execute(stmt)).scalar_one()
        return tenant_loaded, tenant_loaded.api_keys[0], raw_key


@pytest.mark.asyncio
async def test_main_health_and_static_routes():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. /health/live
        live_res = await client.get("/health/live")
        assert live_res.status_code == 200
        assert live_res.json()["status"] == "alive"

        # 2. /health/ready ok
        ready_res = await client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json()["status"] == "ready"

        # 3. /health/ready queue not ready -> 503
        with patch.object(task_queue, "health", new_callable=AsyncMock) as mock_qh:
            mock_qh.return_value = False
            ready_fail = await client.get("/health/ready")
            assert ready_fail.status_code == 503

        # 4. /health/ready DB error -> 503
        with patch("app.main.AsyncSessionLocal", side_effect=Exception("DB Unreachable")):
            ready_db_err = await client.get("/health/ready")
            assert ready_db_err.status_code == 503

        # 5. Static html routes
        for path in ["/", "/portal", "/reconciliation", "/login", "/register"]:
            res_page = await client.get(path)
            assert res_page.status_code == 200


@pytest.mark.asyncio
async def test_database_postgresql_upgrade_branch():
    # Test PostgreSQL migration branch in init_db
    mock_conn = AsyncMock()
    mock_conn.run_sync = AsyncMock()
    mock_conn.execute = AsyncMock()

    mock_engine = MagicMock()
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn

    with patch.object(settings, "DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/cargoplus"), \
         patch("app.database.engine", mock_engine):
        await init_db()
        assert mock_conn.execute.call_count >= 8


@pytest.mark.asyncio
async def test_extract_async_queue_failure_and_upload_branches():
    tenant, api_key, raw_key = await create_active_tenant_and_key()
    tenant_info = (tenant, api_key)

    async with AsyncSessionLocal() as db:
        # 1. extract_async_json queue failure -> 503
        req_json = ExtractAsyncRequest(mail_subject="Fail Dispatch", mail_body="POL: SHANGHAI")
        with patch.object(task_queue, "enqueue", side_effect=RuntimeError("Redis cluster crashed")):
            with pytest.raises(HTTPException) as exc_503:
                await extract_async_json(req_json, tenant_info=tenant_info, db=db)
            assert exc_503.value.status_code == 503

        # 2. extract_async_upload queue failure -> 503
        upload_f1 = UploadFile(filename="test.txt", file=io.BytesIO(b"POL: NINGBO"))
        with patch.object(task_queue, "enqueue", side_effect=RuntimeError("Queue unavailable")):
            with pytest.raises(HTTPException) as exc_upload_503:
                await extract_async_upload(
                    files=[upload_f1],
                    mail_subject="Upload 503",
                    callback_url=None,
                    idempotency_key="idemp_503_test",
                    tenant_info=tenant_info,
                    db=db,
                )
            assert exc_upload_503.value.status_code == 503

        # 3. extract_async_upload duplicate Idempotency-Key
        upload_f2 = UploadFile(filename="test.txt", file=io.BytesIO(b"POL: NINGBO"))
        with patch.object(task_queue, "enqueue", new_callable=AsyncMock):
            res_first = await extract_async_upload(
                files=[upload_f2],
                mail_subject="Upload Idemp",
                callback_url=None,
                idempotency_key="unique_idemp_key_12345",
                tenant_info=tenant_info,
                db=db,
            )
            assert res_first.task_id is not None

        # Duplicate submit
        upload_f3 = UploadFile(filename="test.txt", file=io.BytesIO(b"POL: NINGBO"))
        res_dup = await extract_async_upload(
            files=[upload_f3],
            mail_subject="Upload Idemp",
            callback_url=None,
            idempotency_key="unique_idemp_key_12345",
            tenant_info=tenant_info,
            db=db,
        )
        assert res_dup.task_id == res_first.task_id
        assert "Existing idempotent" in res_dup.message


@pytest.mark.asyncio
async def test_billing_service_remaining_corner_cases():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        tenant = Tenant(
            id=t_id,
            name="BillingCornerTenant",
            balance=Decimal("0.1000"),  # Less than unit_price (0.5000)
            unit_price=Decimal("0.5000"),
            reserved_balance=Decimal("0.0000"),
            is_active=True,
        )
        db.add(tenant)
        await db.commit()

        # 1. reserve_for_new_task when balance < unit_price -> returns None
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task_new = EmailTask(id=task_id, tenant_id=t_id, status="PENDING")
        db.add(task_new)
        await db.commit()

        res_reserve = await BillingService.reserve_for_new_task(db, t_id)
        assert res_reserve is None

        # 2. deduct_for_task_success when unreserved and balance < unit_price -> returns None
        res_deduct_low = await BillingService.deduct_for_task_success(db, t_id, task_id)
        assert res_deduct_low is None

        # 3. update_unit_price out of bounds -> ValueError
        with pytest.raises(ValueError):
            await BillingService.update_unit_price(db, t_id, Decimal("0.001"))  # < MIN_UNIT_PRICE (0.01)

        with pytest.raises(ValueError):
            await BillingService.update_unit_price(db, t_id, Decimal("9999.00"))  # > MAX_UNIT_PRICE (100.00)

        # 4. update_unit_price non-existent tenant -> None
        res_up_none = await BillingService.update_unit_price(db, "non_existent_tenant_999", Decimal("1.00"))
        assert res_up_none is None


@pytest.mark.asyncio
async def test_queue_service_worker_error_handling():
    manager = TaskQueueManager()
    await manager.start()

    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="QueueWorkerErrTenant", balance=Decimal("100.00"), is_active=True)
        task = EmailTask(id=task_id, tenant_id=t_id, status="PENDING", raw_input_json="{}")
        db.add(tenant)
        db.add(task)
        await db.commit()

    with patch("app.services.extraction_service.ExtractionService.process_task", side_effect=Exception("Simulated worker fatal exception")):
        await manager.enqueue(task_id, t_id)
        await asyncio.sleep(0.05)

    await manager.stop()
