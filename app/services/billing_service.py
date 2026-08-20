import asyncio
from decimal import Decimal
import logging
from typing import Dict, List, Optional, Tuple
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tenant import Tenant
from app.models.billing import BillingTransaction
from app.models.task import EmailTask
from app.core.money import (
    MAX_ACCOUNT_BALANCE,
    MAX_RECHARGE_AMOUNT,
    MAX_UNIT_PRICE,
    MONEY_QUANTUM,
    MIN_RECHARGE_AMOUNT,
    MIN_UNIT_PRICE,
    validate_money,
)

logger = logging.getLogger(__name__)

# In-memory tenant locks to ensure serialization of financial transactions under high concurrency
_tenant_locks: Dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


async def _get_tenant_lock(tenant_id: str) -> asyncio.Lock:
    async with _global_lock:
        if tenant_id not in _tenant_locks:
            _tenant_locks[tenant_id] = asyncio.Lock()
        return _tenant_locks[tenant_id]


class BillingService:
    @staticmethod
    async def check_balance_available(db: AsyncSession, tenant_id: str) -> Tuple[bool, Decimal, Decimal]:
        """
        Checks if tenant balance is sufficient for at least one API call.
        Returns: (is_sufficient, current_balance, unit_price)
        """
        stmt = (
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .execution_options(populate_existing=True)
        )
        result = await db.execute(stmt)
        tenant = result.scalar_one_or_none()
        if not tenant:
            return False, Decimal("0.0000"), Decimal("0.5000")

        balance = Decimal(str(tenant.balance))
        reserved = Decimal(str(tenant.reserved_balance))
        unit_price = Decimal(str(tenant.unit_price))
        if (
            not balance.is_finite()
            or not reserved.is_finite()
            or not unit_price.is_finite()
            or balance < 0
            or balance > MAX_ACCOUNT_BALANCE
            or reserved < 0
            or reserved > balance
            or unit_price < MIN_UNIT_PRICE
            or unit_price > MAX_UNIT_PRICE
        ):
            logger.error("Invalid billing state for tenant %s", tenant_id)
            return False, Decimal("0.0000"), unit_price
        available = balance - reserved
        return available >= unit_price, available, unit_price

    @staticmethod
    async def reserve_for_new_task(db: AsyncSession, tenant_id: str) -> Optional[Decimal]:
        """Atomically hold one call's price so queued work cannot exceed available funds."""
        result = await db.execute(
            update(Tenant)
            .where(
                Tenant.id == tenant_id,
                Tenant.is_active.is_(True),
                Tenant.unit_price >= MIN_UNIT_PRICE,
                Tenant.unit_price <= MAX_UNIT_PRICE,
                Tenant.balance >= Decimal("0"),
                Tenant.balance <= MAX_ACCOUNT_BALANCE,
                Tenant.reserved_balance >= Decimal("0"),
                Tenant.reserved_balance <= Tenant.balance,
                Tenant.balance - Tenant.reserved_balance >= Tenant.unit_price,
            )
            .values(reserved_balance=Tenant.reserved_balance + Tenant.unit_price)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        tenant = (
            await db.execute(
                select(Tenant)
                .where(Tenant.id == tenant_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return Decimal(str(tenant.unit_price))

    @staticmethod
    async def release_task_reservation(db: AsyncSession, tenant_id: str, task_id: str) -> bool:
        """Release a held amount inside the caller's current transaction."""
        task = (
            await db.execute(
                select(EmailTask).where(
                    EmailTask.id == task_id,
                    EmailTask.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not task or not task.is_reserved:
            return False
        amount = Decimal(str(task.reserved_amount))
        if (
            not amount.is_finite()
            or amount < MIN_UNIT_PRICE
            or amount > MAX_UNIT_PRICE
        ):
            raise ValueError("Invalid task reservation amount")

        tenant_update = await db.execute(
            update(Tenant)
            .where(
                Tenant.id == tenant_id,
                Tenant.reserved_balance >= amount,
            )
            .values(reserved_balance=Tenant.reserved_balance - amount)
            .execution_options(synchronize_session=False)
        )
        if tenant_update.rowcount != 1:
            raise RuntimeError("Task reservation no longer matches tenant reserved balance")
        task.is_reserved = False
        task.reserved_amount = Decimal("0")
        return True

    @staticmethod
    async def deduct_for_task_success(
        db: AsyncSession,
        tenant_id: str,
        task_id: str,
    ) -> Optional[BillingTransaction]:
        """
        Deducts unit_price for a successfully processed email task.
        Executes within an atomic transaction with serialized per-tenant locking.
        """
        lock = await _get_tenant_lock(tenant_id)
        async with lock:
            task = (
                await db.execute(
                    select(EmailTask).where(
                        EmailTask.id == task_id,
                        EmailTask.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if not task or task.is_charged or task.status not in {"PROCESSING", "SUCCESS"}:
                logger.warning(f"Task {task_id} is already charged. Skipping duplicate deduction.")
                return None

            try:
                # A conditional UPDATE is atomic in both SQLite and PostgreSQL and prevents
                # concurrent workers/processes from taking the balance below zero.
                if task.is_reserved:
                    unit_price = Decimal(str(task.reserved_amount))
                    if (
                        not unit_price.is_finite()
                        or unit_price < MIN_UNIT_PRICE
                        or unit_price > MAX_UNIT_PRICE
                    ):
                        await db.rollback()
                        return None
                    balance_statement = (
                        update(Tenant)
                        .where(
                            Tenant.id == tenant_id,
                            Tenant.is_active.is_(True),
                            Tenant.balance <= MAX_ACCOUNT_BALANCE,
                            Tenant.reserved_balance <= Tenant.balance,
                            Tenant.balance >= unit_price,
                            Tenant.reserved_balance >= unit_price,
                        )
                        .values(
                            balance=Tenant.balance - unit_price,
                            reserved_balance=Tenant.reserved_balance - unit_price,
                        )
                    )
                else:
                    balance_statement = (
                        update(Tenant)
                        .where(
                            Tenant.id == tenant_id,
                            Tenant.is_active.is_(True),
                            Tenant.unit_price >= MIN_UNIT_PRICE,
                            Tenant.unit_price <= MAX_UNIT_PRICE,
                            Tenant.balance >= Decimal("0"),
                            Tenant.balance <= MAX_ACCOUNT_BALANCE,
                            Tenant.reserved_balance >= Decimal("0"),
                            Tenant.reserved_balance <= Tenant.balance,
                            Tenant.balance - Tenant.reserved_balance >= Tenant.unit_price,
                        )
                        .values(balance=Tenant.balance - Tenant.unit_price)
                    )
                balance_update = await db.execute(
                    balance_statement.execution_options(synchronize_session=False)
                )
                if balance_update.rowcount != 1:
                    await db.rollback()
                    logger.warning("Insufficient balance or inactive tenant %s while charging %s", tenant_id, task_id)
                    return None

                tenant = (
                    await db.execute(
                        select(Tenant)
                        .where(Tenant.id == tenant_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if not task.is_reserved:
                    unit_price = Decimal(str(tenant.unit_price))
                balance_after = Decimal(str(tenant.balance))
                balance_before = balance_after + unit_price

                task_update = await db.execute(
                    update(EmailTask)
                    .where(
                        EmailTask.id == task_id,
                        EmailTask.tenant_id == tenant_id,
                        EmailTask.is_charged.is_(False),
                        EmailTask.status.in_(["PROCESSING", "SUCCESS"]),
                    )
                    .values(
                        is_charged=True,
                        charged_amount=unit_price,
                        is_reserved=False,
                        reserved_amount=Decimal("0"),
                    )
                    .execution_options(synchronize_session=False)
                )
                if task_update.rowcount != 1:
                    await db.rollback()
                    return None

                task.is_charged = True
                task.charged_amount = unit_price
                task.is_reserved = False
                task.reserved_amount = Decimal("0")

                tx = BillingTransaction(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    type="DEDUCTION",
                    amount=unit_price,
                    balance_before=balance_before,
                    balance_after=balance_after,
                    description=f"邮件结构化抽取成功扣费 (Task: {task_id})",
                    operator="SYSTEM",
                )
                db.add(tx)
                await db.commit()
                await db.refresh(tx)
                logger.info(
                    "Deducted %s RMB for tenant %s, task %s. Balance: %s",
                    unit_price,
                    tenant_id,
                    task_id,
                    balance_after,
                )
                return tx
            except IntegrityError:
                await db.rollback()
                logger.warning("Duplicate billing transaction prevented for task %s", task_id)
                return None
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    async def recharge_balance(
        db: AsyncSession,
        tenant_id: str,
        amount: Decimal,
        description: str = "人工充值",
        operator: str = "ADMIN",
    ) -> Optional[BillingTransaction]:
        """Recharges balance for a tenant with serialized per-tenant locking."""
        amount = validate_money(
            amount,
            maximum=MAX_RECHARGE_AMOUNT,
            allow_zero=False,
            field_name="Recharge amount",
            minimum=MIN_RECHARGE_AMOUNT,
        )

        lock = await _get_tenant_lock(tenant_id)
        async with lock:
            stored_balance = (
                await db.execute(select(Tenant.balance).where(Tenant.id == tenant_id))
            ).scalar_one_or_none()
            if stored_balance is None:
                return None
            balance_before = Decimal(str(stored_balance))
            if (
                not balance_before.is_finite()
                or balance_before < 0
                or balance_before > MAX_ACCOUNT_BALANCE
            ):
                raise ValueError("Account balance is outside the supported range and requires reconciliation")

            result = await db.execute(
                update(Tenant)
                .where(
                    Tenant.id == tenant_id,
                    Tenant.balance == balance_before,
                    Tenant.balance >= Decimal("0"),
                    Tenant.balance <= MAX_ACCOUNT_BALANCE,
                    Tenant.reserved_balance >= Decimal("0"),
                    Tenant.reserved_balance <= Tenant.balance,
                    Tenant.balance <= MAX_ACCOUNT_BALANCE - amount,
                )
                .values(balance=Tenant.balance + amount)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await db.rollback()
                tenant_balance = (
                    await db.execute(select(Tenant.balance).where(Tenant.id == tenant_id))
                ).scalar_one_or_none()
                if tenant_balance is None:
                    return None
                raise ValueError(
                    f"Recharge would exceed the maximum account balance of {MAX_ACCOUNT_BALANCE}"
                )

            tenant = (
                await db.execute(
                    select(Tenant)
                    .where(Tenant.id == tenant_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            balance_after = Decimal(str(tenant.balance))
            credited_amount = (balance_after - balance_before).quantize(MONEY_QUANTUM)
            if (
                not balance_after.is_finite()
                or balance_after < 0
                or balance_after > MAX_ACCOUNT_BALANCE
                or credited_amount != amount
            ):
                await db.rollback()
                raise ValueError("Recharge did not change the account balance by the requested amount")

            tx = BillingTransaction(
                tenant_id=tenant_id,
                task_id=None,
                type="RECHARGE",
                amount=amount,
                balance_before=balance_before,
                balance_after=balance_after,
                description=description,
                operator=operator,
            )
            db.add(tx)
            await db.commit()
            await db.refresh(tx)
            logger.info(f"Recharged {amount} RMB for tenant {tenant_id}. Balance: {balance_after}")
            return tx

    @staticmethod
    async def update_unit_price(
        db: AsyncSession,
        tenant_id: str,
        new_unit_price: Decimal,
    ) -> Optional[Tenant]:
        """Updates unit price per call for a specific tenant."""
        new_unit_price = validate_money(
            new_unit_price,
            maximum=MAX_UNIT_PRICE,
            allow_zero=False,
            field_name="Unit price",
            minimum=MIN_UNIT_PRICE,
        )
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        result = await db.execute(stmt)
        tenant = result.scalar_one_or_none()
        if not tenant:
            return None

        tenant.unit_price = new_unit_price
        await db.commit()
        await db.refresh(tenant)
        return tenant

    @staticmethod
    async def get_tenant_transactions(
        db: AsyncSession,
        tenant_id: str,
        limit: int = 50,
        transaction_type: Optional[str] = None,
    ) -> List[BillingTransaction]:
        """Queries recent transactions for a tenant."""
        stmt = select(BillingTransaction).where(BillingTransaction.tenant_id == tenant_id)
        if transaction_type:
            stmt = stmt.where(BillingTransaction.type == transaction_type)
        stmt = stmt.order_by(BillingTransaction.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_all_transactions(
        db: AsyncSession,
        limit: int = 50,
        transaction_type: Optional[str] = None,
    ) -> List[BillingTransaction]:
        """Admin queries all recent transactions."""
        stmt = select(BillingTransaction)
        if transaction_type:
            stmt = stmt.where(BillingTransaction.type == transaction_type)
        stmt = stmt.order_by(BillingTransaction.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_tenant_billing_summary(db: AsyncSession, tenant_id: str) -> Dict:
        """Calculates total charges, recharges, and charged task count."""
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        res = await db.execute(stmt)
        tenant = res.scalar_one_or_none()
        if not tenant:
            return {}

        # Aggregate deductions
        deduct_stmt = (
            select(
                func.coalesce(func.sum(BillingTransaction.amount), Decimal("0.0000")).label("total_deducted"),
                func.count(BillingTransaction.id).label("task_count"),
            )
            .where(BillingTransaction.tenant_id == tenant_id)
            .where(BillingTransaction.type == "DEDUCTION")
        )
        deduct_res = await db.execute(deduct_stmt)
        deduct_row = deduct_res.first()
        total_deducted = deduct_row.total_deducted if deduct_row else Decimal("0.0000")
        task_count = deduct_row.task_count if deduct_row else 0

        # Aggregate recharges
        recharge_stmt = (
            select(func.coalesce(func.sum(BillingTransaction.amount), Decimal("0.0000")).label("total_recharged"))
            .where(BillingTransaction.tenant_id == tenant_id)
            .where(BillingTransaction.type == "RECHARGE")
        )
        recharge_res = await db.execute(recharge_stmt)
        recharge_row = recharge_res.first()
        total_recharged = recharge_row.total_recharged if recharge_row else Decimal("0.0000")

        return {
            "tenant_id": tenant_id,
            "current_balance": tenant.balance,
            "reserved_balance": tenant.reserved_balance,
            "available_balance": Decimal(str(tenant.balance)) - Decimal(str(tenant.reserved_balance)),
            "unit_price": tenant.unit_price,
            "total_recharged": total_recharged,
            "total_deducted": total_deducted,
            "total_tasks_charged": task_count,
        }

    @staticmethod
    async def refund_task_deduction(
        db: AsyncSession,
        tenant_id: str,
        task_id: str,
        operator: str = "ADMIN",
        reason: str = "反馈采纳退款冲正",
    ) -> Optional[BillingTransaction]:
        """
        Atomically refunds the task's recorded deduction and stages a REFUND
        transaction in the caller's database transaction.

        The deduction ledger is the source of truth. A task without a positive
        DEDUCTION transaction is never credited with a guessed/default amount.
        The caller owns commit/rollback so the feedback decision and refund are
        persisted as one unit.
        """
        lock = await _get_tenant_lock(tenant_id)
        async with lock:
            # 1. Check if already refunded
            existing_refund_stmt = select(BillingTransaction).where(
                BillingTransaction.task_id == task_id,
                BillingTransaction.type == "REFUND",
            )
            existing_refund = (await db.execute(existing_refund_stmt)).scalar_one_or_none()
            if existing_refund:
                logger.info("Task %s already refunded (tx: %s)", task_id, existing_refund.id)
                return existing_refund

            # 2. Check task charge status and its immutable deduction ledger.
            task_stmt = (
                select(EmailTask)
                .where(EmailTask.id == task_id, EmailTask.tenant_id == tenant_id)
                .execution_options(populate_existing=True)
            )
            task = (await db.execute(task_stmt)).scalar_one_or_none()
            if not task:
                logger.warning("Task %s not found for refund", task_id)
                return None
            if not task.is_charged or Decimal(str(task.charged_amount or 0)) <= 0:
                logger.info("Task %s was not charged; refund skipped", task_id)
                return None

            deduction_stmt = select(BillingTransaction).where(
                BillingTransaction.task_id == task_id,
                BillingTransaction.tenant_id == tenant_id,
                BillingTransaction.type == "DEDUCTION",
            )
            deduction = (await db.execute(deduction_stmt)).scalar_one_or_none()
            if deduction is None:
                raise RuntimeError(
                    f"Charged task {task_id} has no matching deduction transaction"
                )

            refund_amt = validate_money(
                deduction.amount,
                maximum=MAX_RECHARGE_AMOUNT,
                minimum=MIN_RECHARGE_AMOUNT,
                allow_zero=False,
                field_name="refund amount",
            )

            # 3. Atomically increase tenant balance
            balance_update = await db.execute(
                update(Tenant)
                .where(
                    Tenant.id == tenant_id,
                    Tenant.balance >= Decimal("0"),
                    Tenant.balance + refund_amt <= MAX_ACCOUNT_BALANCE,
                )
                .values(balance=Tenant.balance + refund_amt)
                .execution_options(synchronize_session=False)
            )
            if balance_update.rowcount != 1:
                raise RuntimeError(
                    f"Unable to credit refund without exceeding tenant {tenant_id} balance limits"
                )

            tenant = (
                await db.execute(
                    select(Tenant)
                    .where(Tenant.id == tenant_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()

            balance_after = Decimal(str(tenant.balance))
            balance_before = balance_after - refund_amt

            # Create REFUND transaction ledger
            tx = BillingTransaction(
                tenant_id=tenant_id,
                task_id=task_id,
                type="REFUND",
                amount=refund_amt,
                balance_before=balance_before,
                balance_after=balance_after,
                description=f"邮件抽取纠错审核通过退款冲正: {reason} (Task: {task_id})",
                operator=operator,
            )
            db.add(tx)
            await db.flush()
            logger.info("Successfully refunded %s to tenant %s for task %s (tx: %s)", refund_amt, tenant_id, task_id, tx.id)
            return tx
