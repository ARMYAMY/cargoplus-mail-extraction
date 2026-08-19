import re
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.core.money import (
    MAX_RECHARGE_AMOUNT,
    MAX_UNIT_PRICE,
    MIN_RECHARGE_AMOUNT,
    MIN_UNIT_PRICE,
)
from app.core.limits import (
    DEFAULT_TENANT_CONCURRENCY,
    MAX_TENANT_CONCURRENCY,
    MIN_TENANT_CONCURRENCY,
)


class TenantBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=128, description="租户/企业名称")
    contact_email: Optional[str] = Field(None, max_length=128, description="联系人邮箱")
    contact_phone: Optional[str] = Field(None, max_length=64, description="联系人电话")
    unit_price: Decimal = Field(
        default=Decimal("0.5000"),
        ge=MIN_UNIT_PRICE,
        le=MAX_UNIT_PRICE,
        max_digits=10,
        decimal_places=4,
        description="单次成功调用扣费单价（元）",
    )
    max_concurrency: int = Field(
        default=DEFAULT_TENANT_CONCURRENCY,
        ge=MIN_TENANT_CONCURRENCY,
        le=MAX_TENANT_CONCURRENCY,
        strict=True,
        description="最大并发处理任务数",
    )


class TenantCreate(TenantBase):
    initial_balance: Decimal = Field(
        default=Decimal("100.0000"),
        ge=0,
        le=MAX_RECHARGE_AMOUNT,
        max_digits=12,
        decimal_places=4,
        description="初始充值金额（元）",
    )

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("contact_email is not a valid email address")
        return normalized


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    unit_price: Optional[Decimal] = Field(
        None,
        ge=MIN_UNIT_PRICE,
        le=MAX_UNIT_PRICE,
        max_digits=10,
        decimal_places=4,
    )
    max_concurrency: Optional[int] = Field(
        None,
        ge=MIN_TENANT_CONCURRENCY,
        le=MAX_TENANT_CONCURRENCY,
        strict=True,
    )
    is_active: Optional[bool] = None

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("contact_email is not a valid email address")
        return normalized


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    key_prefix: str
    is_active: bool
    last_used_at: Optional[datetime]
    created_at: datetime
    raw_api_key: Optional[str] = None
    api_secret: Optional[str] = None


class TenantResponse(TenantBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    # Output remains readable for legacy SQLite rows that predate input limits.
    # Create/update request models still enforce the NUMERIC(10, 4) boundary.
    unit_price: Decimal
    balance: Decimal
    reserved_balance: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime
    api_keys: List[ApiKeyResponse] = Field(default_factory=list)


class RechargeRequest(BaseModel):
    amount: Decimal = Field(
        ...,
        ge=MIN_RECHARGE_AMOUNT,
        le=MAX_RECHARGE_AMOUNT,
        max_digits=12,
        decimal_places=4,
        description="充值金额（元）",
    )
    description: Optional[str] = Field(default="人工充值", description="充值备注")
    operator: Optional[str] = Field(default="ADMIN", description="操作人")


class UpdateUnitPriceRequest(BaseModel):
    unit_price: Decimal = Field(
        ...,
        ge=MIN_UNIT_PRICE,
        le=MAX_UNIT_PRICE,
        max_digits=10,
        decimal_places=4,
        description="新单价（元/次）",
    )
