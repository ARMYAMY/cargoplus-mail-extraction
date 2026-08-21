import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from sqlalchemy import CheckConstraint, Column, String, Boolean, DateTime, Numeric, Integer, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base
from app.core.limits import DEFAULT_TENANT_CONCURRENCY


def utc_now():
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("balance >= 0 AND balance <= 99999999.9999", name="ck_tenant_balance_range"),
        CheckConstraint(
            "reserved_balance >= 0 AND reserved_balance <= balance",
            name="ck_tenant_reserved_balance_range",
        ),
        CheckConstraint("unit_price >= 0.01 AND unit_price <= 100", name="ck_tenant_unit_price_range"),
        CheckConstraint("max_concurrency >= 1 AND max_concurrency <= 30", name="ck_tenant_concurrency_range"),
    )

    id = Column(String(64), primary_key=True, default=lambda: f"tenant_{uuid.uuid4().hex[:12]}")
    name = Column(String(128), nullable=False, index=True)
    contact_email = Column(String(128), nullable=True, unique=True, index=True)
    contact_phone = Column(String(64), nullable=True)
    password_hash = Column(String(128), nullable=True)
    
    # Financials
    balance = Column(Numeric(12, 4), nullable=False, default=Decimal("0.0000"))
    reserved_balance = Column(Numeric(12, 4), nullable=False, default=Decimal("0.0000"))

    unit_price = Column(Numeric(10, 4), nullable=False, default=Decimal("0.5000"))
    
    # Concurrency control
    max_concurrency = Column(Integer, nullable=False, default=DEFAULT_TENANT_CONCURRENCY)
    
    # Status
    is_active = Column(Boolean, nullable=False, default=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    # Relationships
    api_keys = relationship("ApiKey", back_populates="tenant", cascade="all, delete-orphan", lazy="selectin")
    tasks = relationship("EmailTask", back_populates="tenant", cascade="all, delete-orphan", lazy="selectin")
    billing_transactions = relationship("BillingTransaction", back_populates="tenant", cascade="all, delete-orphan", lazy="selectin")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(64), primary_key=True, default=lambda: f"key_{uuid.uuid4().hex[:12]}")
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False, default="Default Key")
    key_prefix = Column(String(16), nullable=False, index=True)
    key_hash = Column(String(128), nullable=False, unique=True, index=True)
    raw_key = Column(String(128), nullable=True)  # Full raw API Key for user retrieval/copying
    api_secret = Column(String(128), nullable=False)  # Used for webhook HMAC signing
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    tenant = relationship("Tenant", back_populates="api_keys")

    @property
    def raw_api_key(self) -> Optional[str]:
        return self.raw_key or self.key_prefix
