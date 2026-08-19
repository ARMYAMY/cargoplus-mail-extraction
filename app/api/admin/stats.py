from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.billing import BillingTransaction
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.api.deps import verify_admin_access
from app.core.money import MAX_UNIT_PRICE

router = APIRouter(prefix="/admin/stats", dependencies=[Depends(verify_admin_access)])


def utc_now():
    return datetime.now(timezone.utc)


@router.get("", summary="获取系统运行大盘统计数据与近14天趋势")
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
):
    now = utc_now()
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    # 1. Total Tenants
    tenants_count_stmt = select(func.count(Tenant.id))
    total_tenants = (await db.execute(tenants_count_stmt)).scalar() or 0

    active_tenants_stmt = select(func.count(Tenant.id)).where(Tenant.is_active == True)
    active_tenants = (await db.execute(active_tenants_stmt)).scalar() or 0

    # Total Balance across all tenants
    total_balance_stmt = select(func.coalesce(func.sum(Tenant.balance), 0))
    total_balance = (await db.execute(total_balance_stmt)).scalar() or Decimal("0.00")

    # 2. Today's Tasks
    today_total_stmt = select(func.count(EmailTask.id)).where(EmailTask.created_at >= today_start)
    today_total = (await db.execute(today_total_stmt)).scalar() or 0

    today_success_stmt = select(func.count(EmailTask.id)).where(
        EmailTask.created_at >= today_start,
        EmailTask.status == "SUCCESS"
    )
    today_success = (await db.execute(today_success_stmt)).scalar() or 0

    today_failed_stmt = select(func.count(EmailTask.id)).where(
        EmailTask.created_at >= today_start,
        EmailTask.status == "FAILED"
    )
    today_failed = (await db.execute(today_failed_stmt)).scalar() or 0

    today_success_rate = (today_success / today_total * 100) if today_total > 0 else 100.0

    # 3. Today's Revenue (Deductions)
    today_revenue_stmt = select(func.coalesce(func.sum(BillingTransaction.amount), 0)).where(
        BillingTransaction.created_at >= today_start,
        BillingTransaction.type == "DEDUCTION"
    )
    today_revenue = (await db.execute(today_revenue_stmt)).scalar() or Decimal("0.00")

    # 4. Average Latency (Today)
    avg_latency_stmt = select(func.avg(EmailTask.duration_ms)).where(
        EmailTask.created_at >= today_start,
        EmailTask.status == "SUCCESS",
        EmailTask.duration_ms.isnot(None)
    )
    avg_latency = (await db.execute(avg_latency_stmt)).scalar() or 0

    queue_backlog = (
        await db.execute(
            select(func.count(EmailTask.id)).where(EmailTask.status == "PENDING")
        )
    ).scalar_one()
    active_rows = (
        await db.execute(
            select(EmailTask.tenant_id, func.count(EmailTask.id))
            .where(EmailTask.status == "PROCESSING")
            .group_by(EmailTask.tenant_id)
        )
    ).all()
    active_tenants_running = {tenant_id: count for tenant_id, count in active_rows}

    # 5. Past 14 Days History Trend
    history = []
    for i in range(13, -1, -1):
        day_date = (now - timedelta(days=i)).date()
        day_start = datetime(day_date.year, day_date.month, day_date.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)

        d_total_stmt = select(func.count(EmailTask.id)).where(
            EmailTask.created_at >= day_start,
            EmailTask.created_at < day_end
        )
        d_succ_stmt = select(func.count(EmailTask.id)).where(
            EmailTask.created_at >= day_start,
            EmailTask.created_at < day_end,
            EmailTask.status == "SUCCESS"
        )
        d_fail_stmt = select(func.count(EmailTask.id)).where(
            EmailTask.created_at >= day_start,
            EmailTask.created_at < day_end,
            EmailTask.status == "FAILED"
        )
        d_rev_stmt = select(func.coalesce(func.sum(BillingTransaction.amount), 0)).where(
            BillingTransaction.created_at >= day_start,
            BillingTransaction.created_at < day_end,
            BillingTransaction.type == "DEDUCTION"
        )

        d_tot = (await db.execute(d_total_stmt)).scalar() or 0
        d_suc = (await db.execute(d_succ_stmt)).scalar() or 0
        d_fai = (await db.execute(d_fail_stmt)).scalar() or 0
        d_rev = (await db.execute(d_rev_stmt)).scalar() or Decimal("0.00")

        history.append({
            "date": day_date.strftime("%m-%d"),
            "total": d_tot,
            "success": d_suc,
            "failed": d_fai,
            "revenue": float(d_rev),
        })

    return {
        "today_total": today_total,
        "today_success": today_success,
        "today_failed": today_failed,
        "today_success_rate": round(today_success_rate, 1),
        "today_revenue": float(today_revenue),
        "avg_duration_ms": int(avg_latency),
        "queue_backlog": queue_backlog,
        "active_tenants_running": active_tenants_running,
        "total_tenants": total_tenants,
        "active_tenants": active_tenants,
        "total_tenant_balance": float(total_balance),
        "history_14_days": history,
    }


@router.get("/history", summary="获取历史邮件抽取经营分析与趋势")
async def get_historical_dashboard_stats(
    days: int = Query(90, ge=7, le=365, description="趋势统计天数"),
    db: AsyncSession = Depends(get_db),
):
    """Return lifetime KPIs plus a zero-filled daily trend for the selected period."""
    now = utc_now()
    today = now.date()
    period_start_date = today - timedelta(days=days - 1)
    period_start = datetime(
        period_start_date.year,
        period_start_date.month,
        period_start_date.day,
        tzinfo=timezone.utc,
    )

    success_case = case((EmailTask.status == "SUCCESS", 1), else_=0)
    failed_case = case((EmailTask.status == "FAILED", 1), else_=0)
    valid_deduction_filters = (
        BillingTransaction.type == "DEDUCTION",
        BillingTransaction.amount >= Decimal("0"),
        BillingTransaction.amount <= MAX_UNIT_PRICE,
    )

    lifetime_row = (
        await db.execute(
            select(
                func.count(EmailTask.id).label("total"),
                func.coalesce(func.sum(success_case), 0).label("success"),
                func.coalesce(func.sum(failed_case), 0).label("failed"),
                func.avg(
                    case(
                        (
                            (EmailTask.status == "SUCCESS") & EmailTask.duration_ms.isnot(None),
                            EmailTask.duration_ms,
                        )
                    )
                ).label("avg_duration_ms"),
                func.min(EmailTask.created_at).label("first_task_at"),
                func.max(EmailTask.created_at).label("last_task_at"),
            )
        )
    ).one()

    lifetime_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(BillingTransaction.amount), Decimal("0.0000"))).where(
                *valid_deduction_filters
            )
        )
    ).scalar_one()
    revenue_anomaly_count = (
        await db.execute(
            select(func.count(BillingTransaction.id)).where(
                BillingTransaction.type == "DEDUCTION",
                (BillingTransaction.amount < Decimal("0"))
                | (BillingTransaction.amount > MAX_UNIT_PRICE),
            )
        )
    ).scalar_one()

    period_row = (
        await db.execute(
            select(
                func.count(EmailTask.id).label("total"),
                func.coalesce(func.sum(success_case), 0).label("success"),
                func.coalesce(func.sum(failed_case), 0).label("failed"),
            ).where(EmailTask.created_at >= period_start)
        )
    ).one()
    period_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(BillingTransaction.amount), Decimal("0.0000"))).where(
                *valid_deduction_filters,
                BillingTransaction.created_at >= period_start,
            )
        )
    ).scalar_one()

    task_day = func.date(EmailTask.created_at)
    task_daily_rows = (
        await db.execute(
            select(
                task_day.label("day"),
                func.count(EmailTask.id).label("total"),
                func.coalesce(func.sum(success_case), 0).label("success"),
                func.coalesce(func.sum(failed_case), 0).label("failed"),
            )
            .where(EmailTask.created_at >= period_start)
            .group_by(task_day)
            .order_by(task_day)
        )
    ).all()

    revenue_day = func.date(BillingTransaction.created_at)
    revenue_daily_rows = (
        await db.execute(
            select(
                revenue_day.label("day"),
                func.coalesce(func.sum(BillingTransaction.amount), Decimal("0.0000")).label("revenue"),
            )
            .where(
                *valid_deduction_filters,
                BillingTransaction.created_at >= period_start,
            )
            .group_by(revenue_day)
            .order_by(revenue_day)
        )
    ).all()

    daily_map = {
        (period_start_date + timedelta(days=index)).isoformat(): {
            "date": (period_start_date + timedelta(days=index)).isoformat(),
            "total": 0,
            "success": 0,
            "failed": 0,
            "success_rate": 0.0,
            "revenue": 0.0,
        }
        for index in range(days)
    }
    for row in task_daily_rows:
        day = str(row.day)
        if day not in daily_map:
            continue
        completed = int(row.success or 0) + int(row.failed or 0)
        daily_map[day].update(
            {
                "total": int(row.total or 0),
                "success": int(row.success or 0),
                "failed": int(row.failed or 0),
                "success_rate": round((int(row.success or 0) / completed * 100), 1) if completed else 0.0,
            }
        )
    for row in revenue_daily_rows:
        day = str(row.day)
        if day in daily_map:
            daily_map[day]["revenue"] = float(row.revenue or 0)

    tenant_task_rows = (
        await db.execute(
            select(
                Tenant.id.label("tenant_id"),
                Tenant.name.label("tenant_name"),
                func.count(EmailTask.id).label("total"),
                func.coalesce(func.sum(success_case), 0).label("success"),
                func.coalesce(func.sum(failed_case), 0).label("failed"),
            )
            .outerjoin(EmailTask, EmailTask.tenant_id == Tenant.id)
            .group_by(Tenant.id, Tenant.name)
            .order_by(func.count(EmailTask.id).desc())
            .limit(10)
        )
    ).all()
    tenant_revenue_rows = (
        await db.execute(
            select(
                BillingTransaction.tenant_id,
                func.coalesce(func.sum(BillingTransaction.amount), Decimal("0.0000")).label("revenue"),
            )
            .where(*valid_deduction_filters)
            .group_by(BillingTransaction.tenant_id)
        )
    ).all()
    tenant_revenue = {row.tenant_id: float(row.revenue or 0) for row in tenant_revenue_rows}
    tenant_rankings = []
    for row in tenant_task_rows:
        success = int(row.success or 0)
        failed = int(row.failed or 0)
        completed = success + failed
        tenant_rankings.append(
            {
                "tenant_id": row.tenant_id,
                "tenant_name": row.tenant_name,
                "total": int(row.total or 0),
                "success": success,
                "failed": failed,
                "success_rate": round(success / completed * 100, 1) if completed else 0.0,
                "revenue": tenant_revenue.get(row.tenant_id, 0.0),
            }
        )

    lifetime_success = int(lifetime_row.success or 0)
    lifetime_failed = int(lifetime_row.failed or 0)
    lifetime_completed = lifetime_success + lifetime_failed
    lifetime_total = int(lifetime_row.total or 0)
    period_success = int(period_row.success or 0)
    period_failed = int(period_row.failed or 0)
    period_completed = period_success + period_failed

    return {
        "lifetime": {
            "total": lifetime_total,
            "success": lifetime_success,
            "failed": lifetime_failed,
            "in_progress": max(0, lifetime_total - lifetime_completed),
            "success_rate": round(lifetime_success / lifetime_completed * 100, 1) if lifetime_completed else 0.0,
            "revenue": float(lifetime_revenue),
            "avg_duration_ms": int(lifetime_row.avg_duration_ms or 0),
            "avg_revenue_per_success": round(float(lifetime_revenue) / lifetime_success, 4) if lifetime_success else 0.0,
            "revenue_anomaly_count": int(revenue_anomaly_count or 0),
            "first_task_at": lifetime_row.first_task_at,
            "last_task_at": lifetime_row.last_task_at,
        },
        "period": {
            "days": days,
            "start_date": period_start_date.isoformat(),
            "end_date": today.isoformat(),
            "total": int(period_row.total or 0),
            "success": period_success,
            "failed": period_failed,
            "in_progress": max(0, int(period_row.total or 0) - period_completed),
            "success_rate": round(period_success / period_completed * 100, 1) if period_completed else 0.0,
            "revenue": float(period_revenue),
        },
        "history": list(daily_map.values()),
        "tenant_rankings": tenant_rankings,
    }
