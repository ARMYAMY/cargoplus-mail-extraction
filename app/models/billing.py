import uuid
from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, Column, String, DateTime, Numeric, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class BillingTransaction(Base):
    __tablename__ = "billing_transactions"
    __table_args__ = (
        UniqueConstraint("task_id", "type", name="uq_billing_task_type"),
        CheckConstraint("amount >= 0", name="ck_billing_amount_nonnegative"),
        CheckConstraint(
            "balance_before >= 0 AND balance_before <= 99999999.9999 "
            "AND balance_after >= 0 AND balance_after <= 99999999.9999",
            name="ck_billing_balance_range",
        ),
    )

    id = Column(String(64), primary_key=True, default=lambda: f"tx_{uuid.uuid4().hex[:14]}")
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(String(64), ForeignKey("email_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    
    type = Column(String(32), nullable=False)  # DEDUCTION, RECHARGE, REFUND, ADJUSTMENT
    amount = Column(Numeric(12, 4), nullable=False)
    balance_before = Column(Numeric(12, 4), nullable=False)
    balance_after = Column(Numeric(12, 4), nullable=False)
    
    description = Column(String(255), nullable=True)
    operator = Column(String(64), nullable=True, default="SYSTEM")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    tenant = relationship("Tenant", back_populates="billing_transactions")
    task = relationship("EmailTask", back_populates="billing_transactions")
