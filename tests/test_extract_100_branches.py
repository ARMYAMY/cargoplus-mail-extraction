import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.services.auth_service import generate_api_key_and_secret
from app.services.queue_service import task_queue
from app.api.v1.extract import (
    extract_async_json,
    extract_async_upload,
    extract_sync,
    reserve_or_raise,
    enforce_queue_capacity,
    find_idempotent_task,
    ExtractAsyncRequest,
    ExtractSyncRequest,
)


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_active_tenant_and_key():
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name=f"Extract100Tenant {t_id}",
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
async def test_extract_all_validation_and_error_branches():
    tenant, api_key, _ = await create_active_tenant_and_key()
    tenant_info = (tenant, api_key)

    async with AsyncSessionLocal() as db:
        # 1. reserve_or_raise with insufficient balance -> 402 (lines 56-57)
        tenant_poor = Tenant(id=f"tenant_poor_{uuid.uuid4().hex[:6]}", name="Poor", balance=Decimal("0.10"), unit_price=Decimal("0.50"), is_active=True)
        db.add(tenant_poor)
        await db.commit()

        with pytest.raises(HTTPException) as exc_402:
            await reserve_or_raise(db, tenant_poor.id)
        assert exc_402.value.status_code == 402

        # 2. enforce_queue_capacity tenant pending limit -> 429 (line 108)
        with patch.object(settings, "MAX_TENANT_PENDING_TASKS", 0):
            with pytest.raises(HTTPException) as exc_429:
                await enforce_queue_capacity(db, tenant.id)
            assert exc_429.value.status_code == 429

        # 3. extract_async_json with existing idempotent task -> returns duplicate (line 152)
        task_idemp = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant.id,
            idempotency_key="idemp_json_exist_123",
            status="PENDING",
        )
        db.add(task_idemp)
        await db.commit()

        res_idemp = await extract_async_json(
            ExtractAsyncRequest(mail_subject="Test", mail_body="POL: SHANGHAI"),
            idempotency_key="idemp_json_exist_123",
            tenant_info=tenant_info,
            db=db,
        )
        assert res_idemp.task_id == task_idemp.id
        assert "Existing idempotent" in res_idemp.message

        # 4. extract_async_json commit IntegrityError fallback (lines 182-187)
        with patch.object(db, "commit", side_effect=[IntegrityError("stmt", "params", Exception("dup")), None]), \
             patch("app.api.v1.extract.find_idempotent_task", return_value=task_idemp):
            res_commit_dup = await extract_async_json(
                ExtractAsyncRequest(mail_subject="Dup Test", mail_body="POL: SHANGHAI"),
                idempotency_key="idemp_json_exist_123",
                tenant_info=tenant_info,
                db=db,
            )
            assert res_commit_dup.task_id == task_idemp.id

        # 5. extract_async_upload mail_subject > 255 chars -> 422 (line 234)
        f_sample = UploadFile(filename="test.txt", file=io.BytesIO(b"POL: NINGBO"))
        with pytest.raises(HTTPException) as exc_subj_422:
            await extract_async_upload(
                files=[f_sample],
                mail_subject="A" * 300,
                tenant_info=tenant_info,
                db=db,
            )
        assert exc_subj_422.value.status_code == 422

        # 6. extract_async_upload balance insufficient -> 402 (line 243)
        tenant_poor_info = (tenant_poor, api_key)
        f_sample2 = UploadFile(filename="test.txt", file=io.BytesIO(b"POL: NINGBO"))
        with pytest.raises(HTTPException) as exc_up_poor_402:
            await extract_async_upload(
                files=[f_sample2],
                mail_subject="Upload Poor",
                tenant_info=tenant_poor_info,
                db=db,
            )
        assert exc_up_poor_402.value.status_code == 402

        # 7. extract_async_upload len(files) > MAX_UPLOAD_FILES -> 413 (line 256)
        files_many = [UploadFile(filename=f"test_{i}.txt", file=io.BytesIO(b"data")) for i in range(settings.MAX_UPLOAD_FILES + 1)]
        with pytest.raises(HTTPException) as exc_files_many_413:
            await extract_async_upload(
                files=files_many,
                mail_subject="Too Many Files",
                tenant_info=tenant_info,
                db=db,
            )
        assert exc_files_many_413.value.status_code == 413

        # 8. extract_async_upload unsupported file extension -> 415 (line 268)
        f_unsupported = UploadFile(filename="virus.exe", file=io.BytesIO(b"binary"))
        with pytest.raises(HTTPException) as exc_unsupp_415:
            await extract_async_upload(
                files=[f_unsupported],
                mail_subject="Bad Ext",
                tenant_info=tenant_info,
                db=db,
            )
        assert exc_unsupp_415.value.status_code == 415


@pytest.mark.asyncio
async def test_extract_sync_duplicate_integrity_and_sync_polling():
    tenant, api_key, _ = await create_active_tenant_and_key()
    tenant_info = (tenant, api_key)

    async with AsyncSessionLocal() as db:
        # 1. extract_sync commit IntegrityError fallback (lines 419-423)
        task_sync_exist = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant.id,
            idempotency_key="idemp_sync_exist_456",
            status="SUCCESS",
            result_json=json.dumps({"POL": "SHANGHAI"}),
        )
        db.add(task_sync_exist)
        await db.commit()

        with patch.object(db, "commit", side_effect=[IntegrityError("stmt", "params", Exception("dup")), None]), \
             patch("app.api.v1.extract.find_idempotent_task", return_value=task_sync_exist):
            res_sync_dup = await extract_sync(
                ExtractSyncRequest(mail_subject="Sync Dup", mail_body="POL: SHANGHAI"),
                idempotency_key="idemp_sync_exist_456",
                tenant_info=tenant_info,
                db=db,
            )
            assert res_sync_dup["status"] == "SUCCESS"

        # 2. extract_sync Celery mode polling until task completion (lines 440-448)
        task_sync_pending = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant.id,
            idempotency_key="idemp_sync_poll_789",
            status="PENDING",
            result_json=json.dumps({"POL": "NINGBO"}),
        )
        db.add(task_sync_pending)
        await db.commit()

        # Update status to SUCCESS in background
        async def mark_success_soon():
            await asyncio.sleep(0.05)
            async with AsyncSessionLocal() as db2:
                t = await db2.get(EmailTask, task_sync_pending.id)
                t.status = "SUCCESS"
                await db2.commit()

        asyncio.create_task(mark_success_soon())

        with patch.object(settings, "TASK_QUEUE_MODE", "celery"), \
             patch.object(task_queue, "enqueue", new_callable=AsyncMock):
            res_polled = await extract_sync(
                ExtractSyncRequest(mail_subject="Sync Poll", mail_body="POL: NINGBO"),
                idempotency_key="idemp_sync_poll_789",
                tenant_info=tenant_info,
                db=db,
            )
            assert res_polled["status"] == "SUCCESS"
