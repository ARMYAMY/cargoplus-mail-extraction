from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class BillingTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    task_id: Optional[str]
    type: str
    amount: Decimal
    balance_before: Decimal
    balance_after: Decimal
    description: Optional[str]
    operator: Optional[str]
    created_at: datetime


class BillingTransactionListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: List[BillingTransactionResponse] = Field(default_factory=list)


class BillingSummaryResponse(BaseModel):
    tenant_id: str
    current_balance: Decimal
    reserved_balance: Decimal
    available_balance: Decimal
    unit_price: Decimal
    total_recharged: Decimal
    total_deducted: Decimal
    total_tasks_charged: int
    recent_transactions: List[BillingTransactionResponse] = Field(default_factory=list)

