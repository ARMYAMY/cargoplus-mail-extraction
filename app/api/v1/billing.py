import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.billing import BillingTransaction
from app.models.tenant import ApiKey, Tenant
from app.schemas.billing import BillingSummaryResponse, BillingTransactionResponse
from app.api.deps import get_current_tenant_and_key
from app.services.billing_service import BillingService

router = APIRouter()


def _csv_safe(value: Any) -> Any:
    """Prevent spreadsheet software from interpreting exported user text as a formula."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@router.get(
    "/billing/summary",
    response_model=BillingSummaryResponse,
    summary="查询租户账单总览与统计",
)
async def get_billing_summary(
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    summary = await BillingService.get_tenant_billing_summary(db, tenant.id)
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "Tenant not found"},
        )
    return summary


@router.get(
    "/billing/transactions",
    summary="查询租户扣费与充值交易流水 (支持分页)",
)
async def get_billing_transactions(
    tx_type: Optional[str] = Query(None, alias="type", description="流水类型: DEDUCTION, RECHARGE"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    limit: Optional[int] = Query(None, description="兼容旧参数"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    query = select(BillingTransaction).where(BillingTransaction.tenant_id == tenant.id)

    if tx_type:
        query = query.where(BillingTransaction.type == tx_type.upper())

    count_stmt = select(func.count()).select_from(query.subquery())
    total_res = await db.execute(count_stmt)
    total = total_res.scalar() or 0

    effective_size = limit if limit else page_size
    total_pages = max(1, (total + effective_size - 1) // effective_size)

    query = (
        query.order_by(BillingTransaction.created_at.desc())
        .offset((page - 1) * effective_size)
        .limit(effective_size)
    )
    res = await db.execute(query)
    items = res.scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": effective_size,
        "total_pages": total_pages,
        "items": [
            BillingTransactionResponse.model_validate(tx).model_dump(mode="json")
            for tx in items
        ],
    }


@router.get(
    "/billing/statements/daily",
    summary="租户按日对账单汇总 (支持分页)",
)
async def get_daily_statements(
    days: int = Query(30, ge=1, le=90, description="查询天数"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(BillingTransaction)
        .where(
            BillingTransaction.tenant_id == tenant.id,
            BillingTransaction.created_at >= cutoff,
        )
        .order_by(BillingTransaction.created_at.desc())
    )
    res = await db.execute(stmt)
    txs = res.scalars().all()

    # Aggregate by date YYYY-MM-DD
    daily_map: Dict[str, Dict[str, Any]] = {}
    for tx in txs:
        date_str = tx.created_at.strftime("%Y-%m-%d")
        if date_str not in daily_map:
            daily_map[date_str] = {
                "date": date_str,
                "deduction_count": 0,
                "deduction_amount": Decimal("0.0000"),
                "recharge_count": 0,
                "recharge_amount": Decimal("0.0000"),
                "closing_balance": tx.balance_after,
            }
        item = daily_map[date_str]
        if tx.type == "DEDUCTION":
            item["deduction_count"] += 1
            item["deduction_amount"] += tx.amount
        elif tx.type == "RECHARGE":
            item["recharge_count"] += 1
            item["recharge_amount"] += tx.amount

    # Convert to sorted list
    full_list = sorted(daily_map.values(), key=lambda x: x["date"], reverse=True)
    total_records = len(full_list)
    total_pages = max(1, (total_records + page_size - 1) // page_size)
    paginated_items = full_list[(page - 1) * page_size : page * page_size]

    return {
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "unit_price": float(tenant.unit_price),
        "current_balance": float(tenant.balance),
        "total": total_records,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "items": paginated_items,
    }



@router.get(
    "/billing/export-csv",
    summary="导出租户财务对账单 (CSV 格式)",
)
async def export_billing_csv(
    tx_type: Optional[str] = Query(None, alias="type"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    query = select(BillingTransaction).where(BillingTransaction.tenant_id == tenant.id)
    if tx_type:
        query = query.where(BillingTransaction.type == tx_type.upper())
    query = query.order_by(BillingTransaction.created_at.desc()).limit(1000)
    res = await db.execute(query)
    txs = res.scalars().all()

    output = io.StringIO()
    # Write BOM for Excel UTF-8 Chinese compatibility
    output.write("\ufeff")
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "交易流水号 (TxID)",
        "租户ID",
        "业务类型",
        "变动金额 (元)",
        "变动前余额 (元)",
        "变动后余额 (元)",
        "关联任务号 (TaskID)",
        "业务说明 / 备注",
        "操作人",
        "交易时间",
    ])

    for tx in txs:
        writer.writerow([
            tx.id,
            tx.tenant_id,
            "API 扣费" if tx.type == "DEDUCTION" else "账户充值",
            f"-{tx.amount}" if tx.type == "DEDUCTION" else f"+{tx.amount}",
            f"{tx.balance_before}",
            f"{tx.balance_after}",
            tx.task_id or "-",
            _csv_safe(tx.description or ""),
            _csv_safe(tx.operator or "SYSTEM"),
            tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "",
        ])

    csv_data = output.getvalue().encode("utf-8-sig")
    filename = f"cargo_billing_statement_{tenant.id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.csv"

    return StreamingResponse(
        io.BytesIO(csv_data),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
