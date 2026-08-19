import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.services.auth_service import generate_api_key_and_secret
from app.services.extraction_service import ExtractionService
from app.api.admin.tenants import (
    list_all_tenants,
    create_tenant,
    recharge_tenant_direct,
    TenantCreate,
    RechargeRequest,
)


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_admin_tenants_remaining_branches():
    async with AsyncSessionLocal() as db:
        # 1. list_all_tenants
        tenants = await list_all_tenants(db=db)
        assert isinstance(tenants, list)

        # 2. create_tenant with commit IntegrityError -> 409
        with patch.object(db, "commit", side_effect=IntegrityError("stmt", "params", Exception("dup email"))):
            with pytest.raises(HTTPException) as exc_409:
                await create_tenant(
                    TenantCreate(
                        name="IntegrityTenant",
                        contact_email="dup_email@example.com",
                    ),
                    db=db,
                )
            assert exc_409.value.status_code == 409

        # 3. recharge_tenant_direct with non-existent tenant -> 404
        with pytest.raises(HTTPException) as exc_404:
            await recharge_tenant_direct(
                tenant_id="non_existent_tenant_999",
                data=RechargeRequest(amount=Decimal("50.00")),
                db=db,
            )
        assert exc_404.value.status_code == 404

        # 4. recharge_tenant_direct with balance overflow -> 422
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        t_obj = Tenant(id=t_id, name="OverflowTenant", balance=Decimal("99999990.00"), is_active=True)
        db.add(t_obj)
        await db.commit()

        with pytest.raises(HTTPException) as exc_422:
            await recharge_tenant_direct(
                tenant_id=t_id,
                data=RechargeRequest(amount=Decimal("100.00")),
                db=db,
            )
        assert exc_422.value.status_code == 422


@pytest.mark.asyncio
async def test_extraction_service_lease_and_webhook_failure_branches():
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="LeaseTenant", balance=Decimal("100.00"), is_active=True)
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, name="k", key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            raw_input_json=json.dumps({"mail_subject": "Lease", "mail_body": "POL: SHANGHAI", "attachments": []}),
            callback_url="https://api.example.com/webhook",
        )
        db.add(task)
        await db.commit()

    # 1. Lease owner mismatch during success finalization
    # Task lease will be acquired as worker-1, but DB is updated to lease_owner='worker-2' before finalization
    async def mock_extract(*args, **kwargs):
        async with AsyncSessionLocal() as db2:
            t = await db2.get(EmailTask, task_id)
            t.lease_owner = "worker-stolen"
            await db2.commit()
        return {"POL": "SHANGHAI"}

    with patch("app.core.skill_runner.SkillRunner.extract_draft_json", side_effect=mock_extract):
        await ExtractionService.process_task(task_id, lease_owner="worker-1")

    # Task should not be finalized as SUCCESS since lease was stolen
    async with AsyncSessionLocal() as db:
        t_check = await db.get(EmailTask, task_id)
        assert t_check.status != "SUCCESS"

    # 2. Webhook dispatch failure in success step
    task_id2 = f"task_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        task2 = EmailTask(
            id=task_id2,
            tenant_id=t_id,
            status="PENDING",
            raw_input_json=json.dumps({"mail_subject": "Webhook Fail", "mail_body": "POL: SHANGHAI", "attachments": []}),
            callback_url="https://api.example.com/webhook",
        )
        db.add(task2)
        await db.commit()

    with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_draft, \
         patch("app.services.extraction_service.dispatch_webhook", side_effect=RuntimeError("Webhook unreachable")):
        mock_draft.return_value = {"POL": "SHANGHAI"}
        await ExtractionService.process_task(task_id2, lease_owner="worker-2")

    async with AsyncSessionLocal() as db:
        t2_check = await db.get(EmailTask, task_id2)
        assert t2_check.status == "SUCCESS"
        assert t2_check.callback_status == "FAILED"
