import math
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.billing import BillingTransaction
from app.schemas.billing import BillingTransactionListResponse
from app.api.deps import verify_admin_access

router = APIRouter(prefix="/admin/billing", dependencies=[Depends(verify_admin_access)])


@router.get(
    "/transactions",
    response_model=BillingTransactionListResponse,
    summary="管理员全局分页查询财务流水与充值对账记录",
)
async def list_admin_billing_transactions(
    tenant_id: Optional[str] = Query(None, description="按租户ID筛选"),
    tx_type: Optional[str] = Query(None, alias="type", description="按类型筛选: DEDUCTION, RECHARGE"),
    search: Optional[str] = Query(None, description="搜索流水号或关联任务号"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    query = select(BillingTransaction)

    if tenant_id:
        query = query.where(BillingTransaction.tenant_id == tenant_id)
    if tx_type:
        query = query.where(BillingTransaction.type == tx_type.upper())
    if search:
        s = f"%{search.strip()}%"
        query = query.where(
            or_(
                BillingTransaction.id.ilike(s),
                BillingTransaction.task_id.ilike(s),
                BillingTransaction.tenant_id.ilike(s),
                BillingTransaction.description.ilike(s),
            )
        )

    count_stmt = select(func.count()).select_from(query.subquery())
    total_res = await db.execute(count_stmt)
    total = total_res.scalar() or 0

    total_pages = max(1, math.ceil(total / page_size))

    query = (
        query.order_by(BillingTransaction.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    res = await db.execute(query)
    items = res.scalars().all()

    return BillingTransactionListResponse(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        items=items,
    )
