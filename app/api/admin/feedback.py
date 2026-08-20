import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.api.deps import verify_admin_access
from app.models.feedback import BenchmarkCase, FewShotExample, SystemVersion, TaskFeedback
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.schemas.feedback import (
    FewShotCreateRequest,
    FewShotResponse,
    FewShotUpdateRequest,
    SystemVersionReleaseRequest,
    TaskFeedbackReviewRequest,
)
from app.services.billing_service import BillingService
from app.services.evaluation_service import EvaluationService
from app.services.few_shot_service import FewShotService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin_access)], tags=["Admin Feedback & Optimization"])


def utc_now():
    return datetime.now(timezone.utc)


# ==========================================
# 1. Feedbacks Management & Audit
# ==========================================

@router.get("/feedbacks", summary="管理端分页获取纠错反馈列表")
async def list_feedbacks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status: Optional[str] = Query(None, description="状态过滤: PENDING, ACCEPTED, REJECTED, RESOLVED"),
    tenant_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    query = select(TaskFeedback, Tenant.name.label("tenant_name")).outerjoin(Tenant, TaskFeedback.tenant_id == Tenant.id)

    if status:
        query = query.where(TaskFeedback.status == status)
    if tenant_id:
        query = query.where(TaskFeedback.tenant_id == tenant_id)

    count_stmt = select(func.count(TaskFeedback.id))
    if status:
        count_stmt = count_stmt.where(TaskFeedback.status == status)
    if tenant_id:
        count_stmt = count_stmt.where(TaskFeedback.tenant_id == tenant_id)

    total = (await db.execute(count_stmt)).scalar() or 0
    res = await db.execute(query.order_by(TaskFeedback.created_at.desc()).offset(offset).limit(page_size))
    rows = res.all()

    items = []
    for fb, t_name in rows:
        items.append({
            "id": fb.id,
            "task_id": fb.task_id,
            "tenant_id": fb.tenant_id,
            "tenant_name": t_name or fb.tenant_id,
            "status": fb.status,
            "diff_fields": fb.diff_fields or [],
            "diff_fields_count": len(fb.diff_fields or []),
            "error_category": fb.error_category,
            "notes": fb.notes,
            "review_comment": fb.review_comment,
            "reviewed_by": fb.reviewed_by,
            "reviewed_at": fb.reviewed_at.isoformat() if fb.reviewed_at else None,
            "is_refunded": fb.is_refunded,
            "refund_amount": float(fb.refund_amount or 0),
            "refund_tx_id": fb.refund_tx_id,
            "is_benchmark": fb.is_benchmark,
            "resolved_version": fb.resolved_version,
            "created_at": fb.created_at.isoformat(),
        })

    # Summary metrics
    pending_count = (await db.execute(select(func.count(TaskFeedback.id)).where(TaskFeedback.status == "PENDING"))).scalar() or 0
    accepted_count = (await db.execute(select(func.count(TaskFeedback.id)).where(TaskFeedback.status == "ACCEPTED"))).scalar() or 0
    resolved_count = (await db.execute(select(func.count(TaskFeedback.id)).where(TaskFeedback.status == "RESOLVED"))).scalar() or 0

    return {
        "code": 0,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "pending_count": pending_count,
            "accepted_count": accepted_count,
            "resolved_count": resolved_count,
            "items": items,
        },
    }


@router.get("/feedbacks/{feedback_id}", summary="管理端获取单条反馈工单详情与完整Diff")
async def get_feedback_detail(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(TaskFeedback, Tenant.name.label("tenant_name"), EmailTask.mail_subject, EmailTask.input_type, EmailTask.created_at.label("task_time"))
        .outerjoin(Tenant, TaskFeedback.tenant_id == Tenant.id)
        .outerjoin(EmailTask, TaskFeedback.task_id == EmailTask.id)
        .where(TaskFeedback.id == feedback_id)
    )
    res = await db.execute(stmt)
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")

    fb, t_name, subject, input_type, task_time = row

    return {
        "code": 0,
        "data": {
            "id": fb.id,
            "task_id": fb.task_id,
            "task_subject": subject or "-",
            "input_type": input_type or "-",
            "task_time": task_time.isoformat() if task_time else "-",
            "tenant_id": fb.tenant_id,
            "tenant_name": t_name or fb.tenant_id,
            "status": fb.status,
            "original_result": fb.original_result or {},
            "corrected_result": fb.corrected_result or {},
            "diff_fields": fb.diff_fields or [],
            "error_category": fb.error_category,
            "notes": fb.notes,
            "review_comment": fb.review_comment,
            "reviewed_by": fb.reviewed_by,
            "reviewed_at": fb.reviewed_at.isoformat() if fb.reviewed_at else None,
            "is_refunded": fb.is_refunded,
            "refund_amount": float(fb.refund_amount or 0),
            "refund_tx_id": fb.refund_tx_id,
            "is_benchmark": fb.is_benchmark,
            "resolved_version": fb.resolved_version,
            "created_at": fb.created_at.isoformat(),
        },
    }


@router.post("/feedbacks/{feedback_id}/accept", summary="审核采纳反馈 (自动退费冲正 + 自动生成评测用例/Few-Shot)")
async def accept_feedback(
    feedback_id: str,
    payload: TaskFeedbackReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TaskFeedback).where(TaskFeedback.id == feedback_id)
    res = await db.execute(stmt)
    fb = res.scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="工单不存在")

    if fb.status in ["ACCEPTED", "RESOLVED"]:
        return {"code": 0, "message": "该工单此前已被采纳", "data": {"status": fb.status}}

    # 1. Financial Auto-Refund
    refund_tx = None
    if payload.auto_refund and not fb.is_refunded:
        refund_tx = await BillingService.refund_task_deduction(
            db=db,
            tenant_id=fb.tenant_id,
            task_id=fb.task_id,
            operator="ADMIN",
            reason=f"采纳反馈 [{feedback_id}] {payload.review_comment or ''}",
        )
        if refund_tx:
            fb.is_refunded = True
            fb.refund_amount = refund_tx.amount
            fb.refund_tx_id = refund_tx.id

    fb.status = "ACCEPTED"
    fb.error_category = payload.error_category or "UNSPECIFIED"
    fb.review_comment = payload.review_comment or "审核通过并采纳"
    fb.reviewed_by = "admin"
    fb.reviewed_at = utc_now()

    # 2. Convert to BenchmarkCase
    if payload.create_benchmark:
        task_stmt = select(EmailTask).where(EmailTask.id == fb.task_id)
        task = (await db.execute(task_stmt)).scalar_one_or_none()
        bm = BenchmarkCase(
            feedback_id=fb.id,
            doc_type=payload.error_category or "GENERAL",
            title=f"纠错金标用例 (Task: {fb.task_id})",
            input_text=(task.mail_subject + "\n" + (task.input_summary or "")) if task else "",
            ground_truth=fb.corrected_result or {},
            is_active=True,
        )
        db.add(bm)
        fb.is_benchmark = True

    # 3. Convert to FewShot Example
    if payload.create_few_shot:
        task_stmt = select(EmailTask).where(EmailTask.id == fb.task_id)
        task = (await db.execute(task_stmt)).scalar_one_or_none()
        input_sample = (task.mail_subject + "\n" + (task.input_summary or "")) if task else "标准单证输入"
        fs = FewShotExample(
            doc_type=payload.error_category or "GENERAL",
            title=f"纠错样例: {fb.diff_fields[0] if fb.diff_fields else '海运字段纠错'}",
            input_excerpt=input_sample[:1000],
            expected_output=fb.corrected_result or {},
            is_active=True,
            priority=20,
        )
        db.add(fs)
        FewShotService.invalidate_cache()

    await db.commit()
    await db.refresh(fb)

    return {
        "code": 0,
        "message": f"反馈已采纳成功！{'本次调用费用已原路退回至租户账户。' if fb.is_refunded else ''}",
        "data": {
            "feedback_id": fb.id,
            "status": fb.status,
            "is_refunded": fb.is_refunded,
            "refund_amount": float(fb.refund_amount or 0),
            "refund_tx_id": fb.refund_tx_id,
        },
    }


@router.post("/feedbacks/{feedback_id}/reject", summary="驳回反馈工单")
async def reject_feedback(
    feedback_id: str,
    payload: TaskFeedbackReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TaskFeedback).where(TaskFeedback.id == feedback_id)
    res = await db.execute(stmt)
    fb = res.scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="工单不存在")

    fb.status = "REJECTED"
    fb.error_category = payload.error_category or "CLIENT_ERROR"
    fb.review_comment = payload.review_comment or "经核实系统提取结果符合原件，予以驳回"
    fb.reviewed_by = "admin"
    fb.reviewed_at = utc_now()

    await db.commit()
    await db.refresh(fb)

    return {
        "code": 0,
        "message": "反馈工单已驳回关闭",
        "data": {"feedback_id": fb.id, "status": fb.status},
    }


# ==========================================
# 2. Dynamic Few-Shot Knowledge Base CRUD
# ==========================================

@router.get("/few-shots", summary="获取动态 Few-Shot 示例列表")
async def list_few_shots(db: AsyncSession = Depends(get_db)):
    stmt = select(FewShotExample).order_by(FewShotExample.priority.desc(), FewShotExample.created_at.desc())
    res = await db.execute(stmt)
    items = res.scalars().all()
    return {
        "code": 0,
        "data": [
            {
                "id": it.id,
                "doc_type": it.doc_type,
                "title": it.title,
                "input_excerpt": it.input_excerpt,
                "expected_output": it.expected_output,
                "is_active": it.is_active,
                "priority": it.priority,
                "created_at": it.created_at.isoformat(),
            }
            for it in items
        ],
    }


@router.post("/few-shots", summary="新增 Few-Shot 示例")
async def create_few_shot(payload: FewShotCreateRequest, db: AsyncSession = Depends(get_db)):
    fs = FewShotExample(
        doc_type=payload.doc_type,
        title=payload.title,
        input_excerpt=payload.input_excerpt,
        expected_output=payload.expected_output,
        priority=payload.priority,
        is_active=payload.is_active,
    )
    db.add(fs)
    await db.commit()
    await db.refresh(fs)
    FewShotService.invalidate_cache()
    return {"code": 0, "message": "Few-Shot 示例添加成功，已热加载生效", "data": {"id": fs.id}}


@router.put("/few-shots/{fs_id}", summary="修改 Few-Shot 示例")
async def update_few_shot(fs_id: str, payload: FewShotUpdateRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(FewShotExample).where(FewShotExample.id == fs_id)
    res = await db.execute(stmt)
    fs = res.scalar_one_or_none()
    if not fs:
        raise HTTPException(status_code=404, detail="示例不存在")

    if payload.doc_type is not None:
        fs.doc_type = payload.doc_type
    if payload.title is not None:
        fs.title = payload.title
    if payload.input_excerpt is not None:
        fs.input_excerpt = payload.input_excerpt
    if payload.expected_output is not None:
        fs.expected_output = payload.expected_output
    if payload.priority is not None:
        fs.priority = payload.priority
    if payload.is_active is not None:
        fs.is_active = payload.is_active

    await db.commit()
    FewShotService.invalidate_cache()
    return {"code": 0, "message": "Few-Shot 示例更新成功，已热加载生效"}


@router.delete("/few-shots/{fs_id}", summary="删除 Few-Shot 示例")
async def delete_few_shot(fs_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(FewShotExample).where(FewShotExample.id == fs_id)
    res = await db.execute(stmt)
    fs = res.scalar_one_or_none()
    if not fs:
        raise HTTPException(status_code=404, detail="示例不存在")

    await db.delete(fs)
    await db.commit()
    FewShotService.invalidate_cache()
    return {"code": 0, "message": "示例已删除"}


# ==========================================
# 3. Automated Benchmark Regression Evaluation
# ==========================================

@router.post("/evaluation/run", summary="一键执行全量金标回归评测")
async def run_regression_evaluation(db: AsyncSession = Depends(get_db)):
    result = await EvaluationService.run_benchmark_evaluation(db)
    return {
        "code": 0,
        "message": "全量金标回归测试执行完成",
        "data": result,
    }


# ==========================================
# 4. Version Release & Batch Archiving
# ==========================================

@router.post("/version/release", summary="发布新版本并归档已采纳反馈")
async def release_new_version(
    payload: SystemVersionReleaseRequest,
    db: AsyncSession = Depends(get_db),
):
    # Check if version exists
    chk = await db.execute(select(SystemVersion).where(SystemVersion.version_tag == payload.version_tag))
    if chk.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"版本号 {payload.version_tag} 已存在，请更换")

    # Run quick benchmark evaluation for official version badge
    eval_res = await EvaluationService.run_benchmark_evaluation(db)
    benchmark_score = f"{eval_res.get('overall_accuracy_percent', 100.0)}%"

    # Resolve feedbacks
    resolved_count = 0
    if payload.mark_accepted_as_resolved:
        update_stmt = (
            update(TaskFeedback)
            .where(TaskFeedback.status == "ACCEPTED")
            .values(status="RESOLVED", resolved_version=payload.version_tag)
        )
        res_update = await db.execute(update_stmt)
        resolved_count = res_update.rowcount

    ver = SystemVersion(
        version_tag=payload.version_tag,
        benchmark_score=benchmark_score,
        total_test_cases=eval_res.get("total_cases", 0),
        passed_test_cases=eval_res.get("passed_cases", 0),
        changelog=payload.changelog or "优化模型规则与 Few-Shot 样本",
        resolved_feedbacks_count=resolved_count,
        released_by="admin",
    )
    db.add(ver)
    await db.commit()
    await db.refresh(ver)

    return {
        "code": 0,
        "message": f"新版本 {payload.version_tag} 发布成功！已批量将 {resolved_count} 条已采纳工单归档为 [已解决]。",
        "data": {
            "version_tag": ver.version_tag,
            "benchmark_score": ver.benchmark_score,
            "resolved_feedbacks_count": ver.resolved_feedbacks_count,
            "released_at": ver.released_at.isoformat(),
        },
    }


@router.get("/versions", summary="获取历史发布版本列表")
async def list_versions(db: AsyncSession = Depends(get_db)):
    stmt = select(SystemVersion).order_by(SystemVersion.released_at.desc()).limit(20)
    res = await db.execute(stmt)
    versions = res.scalars().all()
    return {
        "code": 0,
        "data": [
            {
                "id": v.id,
                "version_tag": v.version_tag,
                "benchmark_score": v.benchmark_score,
                "total_test_cases": v.total_test_cases,
                "passed_test_cases": v.passed_test_cases,
                "changelog": v.changelog,
                "resolved_feedbacks_count": v.resolved_feedbacks_count,
                "released_at": v.released_at.isoformat(),
            }
            for v in versions
        ],
    }
