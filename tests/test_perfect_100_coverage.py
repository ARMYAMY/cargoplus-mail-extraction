import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.auth_service import generate_api_key_and_secret
from app.services.billing_service import BillingService
from app.services.storage_service import StorageService
from app.services.extraction_service import ExtractionService
from app.api.admin.recharge import recharge_tenant, RechargeRequest
from app.api.admin.tasks import retry_task, _format_task_response


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_admin_recharge_error_branches():
    async with AsyncSessionLocal() as db:
        # 1. 404 for non-existent tenant
        with pytest.raises(HTTPException) as exc_404:
            await recharge_tenant(
                tenant_id="non_existent_tenant_999",
                data=RechargeRequest(amount=Decimal("100.00")),
                db=db,
            )
        assert exc_404.value.status_code == 404

        # 2. 422 for overflow recharge amount (balance + amount > 99999999.9999)
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        tenant = Tenant(id=t_id, name="RechargeOverflowTenant", balance=Decimal("99999990.00"), is_active=True)
        db.add(tenant)
        await db.commit()

        with pytest.raises(HTTPException) as exc_422:
            await recharge_tenant(
                tenant_id=t_id,
                data=RechargeRequest(amount=Decimal("100.00")),
                db=db,
            )
        assert exc_422.value.status_code == 422


@pytest.mark.asyncio
async def test_admin_tasks_retry_and_format_branches():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        tenant = Tenant(id=t_id, name="AdminTaskRetryTenant", balance=Decimal("0.00"), unit_price=Decimal("1.00"), is_active=True)
        db.add(tenant)
        await db.commit()

        # 1. 404 for unknown task retry
        with pytest.raises(HTTPException) as exc_404:
            await retry_task("non_existent_task_999", db=db)
        assert exc_404.value.status_code == 404

        # 2. 409 for non-failed task retry
        task_succ = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="SUCCESS",
            input_type="JSON",
            charged_amount=Decimal("0.50"),
            is_charged=True,
            reserved_amount=Decimal("0.50"),
            is_reserved=False,
            callback_status="NONE",
            created_at=datetime.now(timezone.utc),
        )
        db.add(task_succ)
        await db.commit()

        with pytest.raises(HTTPException) as exc_409:
            await retry_task(task_succ.id, db=db)
        assert exc_409.value.status_code == 409

        # 3. 402 for failed task retry when balance is insufficient
        task_failed = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="FAILED",
            input_type="JSON",
            charged_amount=Decimal("0.00"),
            is_charged=False,
            reserved_amount=Decimal("0.00"),
            is_reserved=False,
            callback_status="NONE",
            created_at=datetime.now(timezone.utc),
        )
        db.add(task_failed)
        await db.commit()

        with pytest.raises(HTTPException) as exc_402:
            await retry_task(task_failed.id, db=db)
        assert exc_402.value.status_code == 402

        # 4. _format_task_response with corrupted json
        task_corrupt = EmailTask(
            id="task_corrupt",
            tenant_id=t_id,
            status="SUCCESS",
            input_type="JSON",
            charged_amount=Decimal("0.50"),
            is_charged=True,
            reserved_amount=Decimal("0.50"),
            is_reserved=False,
            callback_status="NONE",
            created_at=datetime.now(timezone.utc),
            result_json="invalid{json",
        )
        resp = _format_task_response(task_corrupt)
        assert resp.result_json is None


def test_storage_service_all_branches():
    # 1. days <= 0 -> ValueError
    with pytest.raises(ValueError):
        StorageService.prune_expired_uploads(days=0)

    # 2. Non-existent upload_dir -> 0
    with patch.object(settings, "UPLOAD_DIR", "non_existent_path_xyz_999"):
        assert StorageService.prune_expired_uploads(days=30) == 0

    # 3. os.walk exception handling
    with patch("os.walk", side_effect=PermissionError("Access Denied")):
        assert StorageService.prune_expired_uploads(days=30) == 0


@pytest.mark.asyncio
async def test_extraction_service_edge_branches():
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="ExtractEdgeTenant", balance=Decimal("100.00"), is_active=True)
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, name="k", key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. Extraction task validation error in process_task
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            raw_input_json=json.dumps({"mail_subject": "Edge", "mail_body": "POL: SHANGHAI", "attachments": []}),
            callback_url="https://api.example.com/webhook",
        )
        db.add(task)
        await db.commit()

    # Normalizer produces valid or invalid output -> test validation failure path
    with patch("app.core.validator.CargoValidator.validate", return_value=(False, ["Invalid Schema format"])), \
         patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_llm, \
         patch("app.celery_tasks.send_webhook_notification", new_callable=AsyncMock):
        mock_llm.return_value = {"POL": "SHANGHAI"}
        await ExtractionService.process_task(task_id, lease_owner="worker-1")

    async with AsyncSessionLocal() as db:
        t_res = await db.get(EmailTask, task_id)
        assert t_res.status == "FAILED"
        assert "validation" in t_res.error_message.lower()
