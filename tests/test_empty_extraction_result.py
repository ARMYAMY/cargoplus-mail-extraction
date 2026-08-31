import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.database import AsyncSessionLocal, init_db
from app.models.feedback import AdminJob
from app.models.task import EmailTask
from app.models.tenant import ApiKey, Tenant
from app.schemas.cargo_v3 import CargoV3Output
from app.services.auth_service import generate_api_key_and_secret
from app.services.admin_job_service import AdminJobService
from app.services.extraction_service import (
    EMPTY_EXTRACTION_MESSAGE,
    EmptyExtractionResultError,
    ExtractionService,
)


def test_only_default_goods_type_is_empty_extraction():
    result = CargoV3Output().model_dump()
    with pytest.raises(EmptyExtractionResultError, match="模型返回空提取结果"):
        ExtractionService.ensure_meaningful_result(result)

    result["POLName"] = "SHANGHAI"
    ExtractionService.ensure_meaningful_result(result)


@pytest.mark.asyncio
async def test_admin_job_marks_empty_extraction_as_failed_and_retryable():
    await init_db()
    job_id = f"job_empty_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        db.add(AdminJob(id=job_id, job_type="PROMPT_SINGLE_EVALUATION"))
        await db.commit()

    async def fail_with_empty_result(_job_id):
        raise EmptyExtractionResultError(EMPTY_EXTRACTION_MESSAGE)

    await AdminJobService._run_guarded(job_id, fail_with_empty_result)

    async with AsyncSessionLocal() as db:
        job = await db.get(AdminJob, job_id)
        assert job.status == "FAILED"
        assert job.error_code == "EMPTY_RESPONSE"
        assert "重试" in job.error_message


@pytest.mark.asyncio
async def test_empty_extraction_marks_task_failed_without_charge():
    await init_db()
    tenant_id = f"tenant_empty_{uuid.uuid4().hex[:8]}"
    task_id = f"task_empty_{uuid.uuid4().hex[:8]}"
    _raw_key, prefix, key_hash, secret = generate_api_key_and_secret()

    async with AsyncSessionLocal() as db:
        db.add(Tenant(
            id=tenant_id,
            name="Empty Result Tenant",
            balance=Decimal("10.0000"),
            reserved_balance=Decimal("0.5000"),
            unit_price=Decimal("0.5000"),
            is_active=True,
        ))
        db.add(ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            name="test",
            key_prefix=prefix,
            key_hash=key_hash,
            api_secret=secret,
            is_active=True,
        ))
        db.add(EmailTask(
            id=task_id,
            tenant_id=tenant_id,
            status="PENDING",
            input_type="JSON",
            raw_input_json=json.dumps({
                "mail_subject": "Empty extraction",
                "mail_body": "no recognizable cargo fields",
                "attachments": [],
            }),
            reserved_amount=Decimal("0.5000"),
            is_reserved=True,
        ))
        await db.commit()

    with patch(
        "app.core.skill_runner.SkillRunner.extract_draft_json",
        new=AsyncMock(return_value={}),
    ):
        await ExtractionService.process_task(task_id, lease_owner="empty-test-worker")

    async with AsyncSessionLocal() as db:
        task = await db.get(EmailTask, task_id)
        tenant = await db.get(Tenant, tenant_id)
        assert task.status == "FAILED"
        assert EMPTY_EXTRACTION_MESSAGE in task.error_message
        assert task.is_charged is False
        assert task.charged_amount == Decimal("0.0000")
        assert task.is_reserved is False
        assert tenant.balance == Decimal("10.0000")
        assert tenant.reserved_balance == Decimal("0.0000")
