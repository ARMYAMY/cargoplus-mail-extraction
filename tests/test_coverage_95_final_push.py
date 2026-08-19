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
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import seed_initial_demo_tenant
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.auth_service import generate_api_key_and_secret
from app.services.queue_service import task_queue
from app.services.storage_service import StorageService
from app.services.webhook_dispatcher import dispatch_webhook
from app.celery_tasks import _deliver_task_webhook, deliver_task_webhook, process_email_task
from app.api.v1.extract import extract_sync, extract_async_upload, ExtractSyncRequest


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_active_tenant_and_key():
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name=f"FinalPushTenant {t_id}",
            balance=Decimal("200.00"),
            unit_price=Decimal("0.50"),
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

        stmt = select(Tenant).options(selectinload(Tenant.api_keys)).where(Tenant.id == t_id)
        t_loaded = (await db.execute(stmt)).scalar_one()
        return t_loaded, t_loaded.api_keys[0], raw_key


@pytest.mark.asyncio
async def test_webhook_dispatcher_and_celery_task():
    tenant, api_key, _ = await create_active_tenant_and_key()
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        task = EmailTask(
            id=task_id,
            tenant_id=tenant.id,
            api_key_id=api_key.id,
            status="SUCCESS",
            callback_url="https://api.example.com/webhook",
            result_json=json.dumps({"POL": "SHANGHAI"}),
            duration_ms=100,
            charged_amount=Decimal("0.50"),
        )
        db.add(task)
        await db.commit()

        # 1. dispatch_webhook in Celery mode
        with patch.object(settings, "TASK_QUEUE_MODE", "celery"), \
             patch("app.celery_tasks.deliver_task_webhook.apply_async") as mock_apply:
            res_disp = await dispatch_webhook(
                db=db,
                task_id=task_id,
                callback_url=task.callback_url,
                tenant_secret=api_key.api_secret,
                payload={"test": 1},
            )
            assert res_disp == "PENDING"
            assert mock_apply.called

        # 2. _deliver_task_webhook normal execution
        with patch("app.celery_tasks.send_webhook_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            await _deliver_task_webhook(task_id)

            async with AsyncSessionLocal() as db2:
                t_reloaded = await db2.get(EmailTask, task_id)
                assert t_reloaded.callback_status == "SUCCESS"

        # 3. deliver_task_webhook wrapper
        with patch("app.celery_tasks.run_async") as mock_run:
            deliver_task_webhook(task_id)
            assert mock_run.called

        # 4. _deliver_task_webhook with missing task / no callback
        await _deliver_task_webhook("non_existent_task_999")


@pytest.mark.asyncio
async def test_extract_sync_celery_mode_branches():
    tenant, api_key, _ = await create_active_tenant_and_key()
    tenant_info = (tenant, api_key)

    async with AsyncSessionLocal() as db:
        # 1. Celery mode with queue unavailable -> 503
        req_sync = ExtractSyncRequest(mail_subject="Sync Celery", mail_body="POL: SHANGHAI")
        with patch.object(settings, "TASK_QUEUE_MODE", "celery"), \
             patch.object(task_queue, "enqueue", side_effect=RuntimeError("Redis down")):
            with pytest.raises(HTTPException) as exc_503:
                await extract_sync(req_sync, tenant_info=tenant_info, db=db)
            assert exc_503.value.status_code == 503

        # 2. Celery mode with completed task
        with patch.object(settings, "TASK_QUEUE_MODE", "celery"), \
             patch.object(task_queue, "enqueue", new_callable=AsyncMock):
            # Create pre-existing success task
            task_succ = EmailTask(
                id=f"task_{uuid.uuid4().hex[:8]}",
                tenant_id=tenant.id,
                idempotency_key="sync_idemp_success",
                status="SUCCESS",
                result_json=json.dumps({"POL": "SHANGHAI"}),
            )
            db.add(task_succ)
            await db.commit()

            req_succ = ExtractSyncRequest(mail_subject="Sync Success", mail_body="POL: SHANGHAI")
            res_sync_ok = await extract_sync(req_succ, idempotency_key="sync_idemp_success", tenant_info=tenant_info, db=db)
            assert res_sync_ok["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_extract_async_upload_size_limits_and_rollback():
    tenant, api_key, _ = await create_active_tenant_and_key()
    tenant_info = (tenant, api_key)

    async with AsyncSessionLocal() as db:
        # 1. Total upload size exceeding MAX_UPLOAD_TOTAL_SIZE -> 413
        f1 = MagicMock(spec=UploadFile)
        f1.filename = "large1.txt"
        f1.read = AsyncMock(side_effect=[b"A" * (settings.MAX_UPLOAD_TOTAL_SIZE + 1024), b""])
        f1.close = AsyncMock()

        with pytest.raises(HTTPException) as exc_tot_large:
            await extract_async_upload(
                files=[f1],
                mail_subject="Total Large",
                callback_url=None,
                tenant_info=tenant_info,
                db=db,
            )
        assert exc_tot_large.value.status_code == 413

        # 2. Upload commit failure unlinking saved files
        f_valid = MagicMock(spec=UploadFile)
        f_valid.filename = "valid_save.txt"
        f_valid.read = AsyncMock(side_effect=[b"POL: SHANGHAI", b""])
        f_valid.close = AsyncMock()

        with patch("app.api.v1.extract.reserve_or_raise", side_effect=Exception("Database Fatal Error")):
            with pytest.raises(Exception):
                await extract_async_upload(
                    files=[f_valid],
                    mail_subject="DB Fail",
                    callback_url=None,
                    tenant_info=tenant_info,
                    db=db,
                )


@pytest.mark.asyncio
async def test_seed_initial_demo_tenant_execution():
    # Force empty tenant query result to trigger demo tenant seed
    mock_db = AsyncMock()
    mock_db.__aenter__.return_value = mock_db
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_res
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    with patch("app.main.AsyncSessionLocal", return_value=mock_db), \
         patch("app.services.billing_service.BillingService.recharge_balance", new_callable=AsyncMock):
        await seed_initial_demo_tenant()
        assert mock_db.commit.called
