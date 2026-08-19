from decimal import Decimal
import uuid
import pytest
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant
from app.models.task import EmailTask
from app.services.billing_service import BillingService


@pytest.mark.asyncio
async def test_billing_lifecycle():
    await init_db()
    tenant_id = f"tenant_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        # Create a test tenant
        tenant = Tenant(
            id=tenant_id,
            name="测试账单公司",
            balance=Decimal("1.0000"),
            unit_price=Decimal("0.5000"),
        )
        db.add(tenant)
        await db.commit()

        # Check balance
        is_ok, bal, price = await BillingService.check_balance_available(db, tenant_id)
        assert is_ok is True
        assert bal == Decimal("1.0000")
        assert price == Decimal("0.5000")

        # Create a test task
        task_id_1 = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id_1,
            tenant_id=tenant_id,
            status="SUCCESS",
        )
        db.add(task)
        await db.commit()

        # Deduct 1st call
        tx1 = await BillingService.deduct_for_task_success(db, tenant_id, task_id_1)
        assert tx1 is not None
        assert tx1.amount == Decimal("0.5000")
        assert tx1.balance_after == Decimal("0.5000")

        # Deduct 2nd call
        task_id_2 = f"task_{uuid.uuid4().hex[:8]}"
        task2 = EmailTask(id=task_id_2, tenant_id=tenant_id, status="SUCCESS")
        db.add(task2)
        await db.commit()
        tx2 = await BillingService.deduct_for_task_success(db, tenant_id, task_id_2)
        assert tx2.balance_after == Decimal("0.0000")

        # Check balance after 2 deductions -> now 0.00, should be insufficient
        is_ok_3, bal_3, _ = await BillingService.check_balance_available(db, tenant_id)
        assert is_ok_3 is False
        assert bal_3 == Decimal("0.0000")

        # Recharge 50.00 RMB
        recharge_tx = await BillingService.recharge_balance(
            db, tenant_id, Decimal("50.0000"), description="在线充值", operator="ADMIN"
        )
        assert recharge_tx.balance_after == Decimal("50.0000")

        # Check balance again -> now sufficient
        is_ok_4, bal_4, _ = await BillingService.check_balance_available(db, tenant_id)
        assert is_ok_4 is True
        assert bal_4 == Decimal("50.0000")
