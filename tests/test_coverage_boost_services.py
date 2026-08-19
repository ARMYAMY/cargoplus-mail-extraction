import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio

from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.billing_service import BillingService
from app.services.extraction_service import ExtractionService
from app.services.storage_service import StorageService
from app.services.auth_service import hash_password, generate_api_key_and_secret


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_test_tenant(db, name, email, balance=Decimal("100.00"), unit_price=Decimal("1.50")):
    tenant_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, key_prefix, key_hash, api_secret = generate_api_key_and_secret()
    tenant = Tenant(
        id=tenant_id,
        name=name,
        contact_email=email,
        password_hash=hash_password("pwd123456"),
        balance=balance,
        unit_price=unit_price,
        is_active=True,
    )
    api_key = ApiKey(
        id=f"key_{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        api_secret=api_secret,
        name="默认密钥",
        is_active=True,
    )
    db.add(tenant)
    db.add(api_key)
    await db.commit()
    return tenant, raw_key


@pytest.mark.asyncio
async def test_billing_service_comprehensive_methods():
    async with AsyncSessionLocal() as db:
        tenant, _ = await create_test_tenant(db, "BillingSvcCo", "billingsvc@example.com")
        t_id = tenant.id

        # 1. reserve_for_new_task
        reserved_amt = await BillingService.reserve_for_new_task(db, t_id)
        assert reserved_amt == Decimal("1.5000")

        # 2. release_task_reservation
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            is_reserved=True,
            reserved_amount=Decimal("1.5000"),
        )
        db.add(task)
        await db.commit()

        rel_success = await BillingService.release_task_reservation(db, t_id, task_id)
        assert rel_success is True

        # Release non-reserved task -> False
        assert await BillingService.release_task_reservation(db, t_id, "non_reserved_task") is False

        # 3. deduct_for_task_success
        task2_id = f"task_{uuid.uuid4().hex[:8]}"
        task2 = EmailTask(
            id=task2_id,
            tenant_id=t_id,
            status="PROCESSING",
            is_reserved=False,
            reserved_amount=Decimal("0.0000"),
        )
        db.add(task2)
        await db.commit()

        tx = await BillingService.deduct_for_task_success(db, t_id, task2_id)
        assert tx is not None
        assert tx.type == "DEDUCTION"
        assert tx.amount == Decimal("1.5000")

        # 4. update_unit_price
        updated_t = await BillingService.update_unit_price(db, t_id, Decimal("2.0000"))
        assert updated_t is not None
        assert updated_t.unit_price == Decimal("2.0000")

        # Update non-existent tenant
        assert await BillingService.update_unit_price(db, "non_existent_tenant", Decimal("1.00")) is None

        # 5. get_tenant_transactions & get_all_transactions
        t_txs = await BillingService.get_tenant_transactions(db, t_id, limit=10)
        assert len(t_txs) >= 1

        all_txs = await BillingService.get_all_transactions(db, limit=10, transaction_type="DEDUCTION")
        assert len(all_txs) >= 1

        # 6. get_tenant_billing_summary
        summary = await BillingService.get_tenant_billing_summary(db, t_id)
        assert summary["tenant_id"] == t_id
        assert summary["total_tasks_charged"] >= 1

        # Non-existent summary
        none_summary = await BillingService.get_tenant_billing_summary(db, "none_tenant")
        assert none_summary == {}


@pytest.mark.asyncio
async def test_extraction_service_pipeline_and_failures():
    async with AsyncSessionLocal() as db:
        tenant, _ = await create_test_tenant(db, "ExtPipeCo", "extpipe@example.com")
        t_id = tenant.id

        # 1. Process task when task is already SUCCESS (not PENDING) -> skips
        task_done_id = f"task_{uuid.uuid4().hex[:8]}"
        task_done = EmailTask(id=task_done_id, tenant_id=t_id, status="SUCCESS")
        db.add(task_done)
        await db.commit()

        await ExtractionService.process_task(task_done_id)

        # 2. Process task with inactive tenant
        t_inactive, _ = await create_test_tenant(db, "InactiveCo", "inact@example.com")
        t_inactive.is_active = False
        task_inact_id = f"task_{uuid.uuid4().hex[:8]}"
        task_inact = EmailTask(
            id=task_inact_id,
            tenant_id=t_inactive.id,
            status="PENDING",
            is_reserved=True,
            reserved_amount=Decimal("1.5000"),
        )
        t_inactive.reserved_balance = Decimal("1.5000")
        db.add(task_inact)
        await db.commit()

        await ExtractionService.process_task(task_inact_id)

        # Verify task is marked FAILED
        async with AsyncSessionLocal() as check_db:
            t_check = await check_db.get(EmailTask, task_inact_id)
            assert t_check.status == "FAILED"

        # 3. Process task with normal LLM extraction and validation
        task_ok_id = f"task_{uuid.uuid4().hex[:8]}"
        task_ok = EmailTask(
            id=task_ok_id,
            tenant_id=t_id,
            status="PENDING",
            mail_subject="MSC Booking Confirmation",
            raw_input_json=json.dumps({
                "mail_subject": "MSC Booking",
                "mail_body": "POL: SHANGHAI POD: HAMBURG",
                "attachments": [],
            }),
            is_reserved=True,
            reserved_amount=Decimal("1.5000"),
        )
        tenant.reserved_balance = Decimal("1.5000")
        db.add(task_ok)
        await db.commit()

        with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "ShipperName": "COSCO SHIPPING",
                "POL": "SHANGHAI",
                "POD": "HAMBURG",
                "ContainerInfo": [],
            }
            await ExtractionService.process_task(task_ok_id, tenant_secret="secret_123")

        async with AsyncSessionLocal() as check_db:
            t_ok_check = await check_db.get(EmailTask, task_ok_id)
            assert t_ok_check.status == "SUCCESS"
            assert t_ok_check.is_charged is True
