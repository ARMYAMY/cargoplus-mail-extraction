import json
import logging
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.feedback import TaskFeedback
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.schemas.feedback import TaskFeedbackCreateRequest, TaskFeedbackResponse
from app.api.deps import get_current_tenant_and_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks/{task_id}/feedback", tags=["Task Feedback"])


def compute_json_diff_fields(original: Dict[str, Any], corrected: Dict[str, Any]) -> List[str]:
    """Computes top-level and nested key differences between original and corrected JSON."""
    diff_keys = set()
    all_keys = set(original.keys()) | set(corrected.keys())
    for k in all_keys:
        orig_val = original.get(k)
        corr_val = corrected.get(k)
        if orig_val != corr_val:
            diff_keys.add(k)
    return sorted(list(diff_keys))


@router.post("", response_model=Dict[str, Any], summary="租户/客户端提交任务字段纠错反馈")
async def submit_task_feedback(
    task_id: str,
    payload: TaskFeedbackCreateRequest,
    tenant_info: tuple[Tenant, Any] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info

    # 1. Look up task
    task_stmt = select(EmailTask).where(EmailTask.id == task_id)
    task_res = await db.execute(task_stmt)
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="指定任务不存在")

    if task.tenant_id != tenant.id:
        raise HTTPException(status_code=403, detail="无权操作其他租户的任务")

    orig_json = {}
    if isinstance(task.result_json, str):
        try:
            orig_json = json.loads(task.result_json)
        except Exception:
            orig_json = {}
    elif isinstance(task.result_json, dict):
        orig_json = task.result_json

    corr_json = payload.corrected_result or {}

    diff_fields = compute_json_diff_fields(orig_json, corr_json)

    # 2. Check if existing feedback exists
    fb_stmt = select(TaskFeedback).where(TaskFeedback.task_id == task_id)
    fb_res = await db.execute(fb_stmt)
    feedback = fb_res.scalar_one_or_none()

    if feedback:
        if feedback.status in ["ACCEPTED", "RESOLVED"]:
            raise HTTPException(status_code=400, detail="该任务纠错反馈已被采纳处理，无需重复提交")
        feedback.original_result = orig_json
        feedback.corrected_result = corr_json
        feedback.diff_fields = diff_fields
        feedback.notes = payload.notes
        feedback.status = "PENDING"
    else:
        feedback = TaskFeedback(
            task_id=task_id,
            tenant_id=tenant.id,
            status="PENDING",
            original_result=orig_json,
            corrected_result=corr_json,
            diff_fields=diff_fields,
            notes=payload.notes,
        )
        db.add(feedback)

    await db.commit()
    await db.refresh(feedback)

    return {
        "code": 0,
        "message": "纠错反馈提交成功，管理端审核确认后将自动退还本次调用费用并优化模型规则",
        "data": {
            "feedback_id": feedback.id,
            "task_id": feedback.task_id,
            "status": feedback.status,
            "diff_fields_count": len(diff_fields),
            "diff_fields": diff_fields,
            "created_at": feedback.created_at.isoformat(),
        },
    }


@router.get("", response_model=Dict[str, Any], summary="查询任务纠错反馈状态")
async def get_task_feedback_status(
    task_id: str,
    tenant_info: tuple[Tenant, Any] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info

    fb_stmt = select(TaskFeedback).where(TaskFeedback.task_id == task_id, TaskFeedback.tenant_id == tenant.id)
    fb_res = await db.execute(fb_stmt)
    feedback = fb_res.scalar_one_or_none()
    if not feedback:
        return {"code": 0, "message": "暂无反馈记录", "data": None}

    return {
        "code": 0,
        "data": {
            "id": feedback.id,
            "status": feedback.status,
            "diff_fields": feedback.diff_fields or [],
            "error_category": feedback.error_category,
            "is_refunded": feedback.is_refunded,
            "refund_amount": float(feedback.refund_amount or 0),
            "review_comment": feedback.review_comment,
            "reviewed_at": feedback.reviewed_at.isoformat() if feedback.reviewed_at else None,
            "resolved_version": feedback.resolved_version,
            "created_at": feedback.created_at.isoformat(),
        },
    }
