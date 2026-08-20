import json
import logging
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.feedback import TaskFeedback
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.schemas.cargo_v3 import CargoV3Output
from app.schemas.feedback import TaskFeedbackCreateRequest
from app.api.deps import get_current_tenant_and_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks/{task_id}/feedback", tags=["Task Feedback"])
MAX_FEEDBACK_JSON_BYTES = 64 * 1024


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

    # Lock the tenant-owned task so concurrent submissions cannot create two
    # feedback rows for the same extraction.
    task_stmt = (
        select(EmailTask)
        .where(EmailTask.id == task_id, EmailTask.tenant_id == tenant.id)
        .with_for_update()
    )
    task_res = await db.execute(task_stmt)
    task = task_res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="指定任务不存在")
    if task.status not in {"SUCCESS", "FAILED"}:
        raise HTTPException(status_code=409, detail="任务尚未结束，暂不能提交纠错反馈")

    orig_json = {}
    if isinstance(task.result_json, str):
        try:
            orig_json = json.loads(task.result_json)
        except Exception:
            orig_json = {}
    elif isinstance(task.result_json, dict):
        orig_json = task.result_json
    if not isinstance(orig_json, dict):
        orig_json = {}

    corrected_patch = payload.corrected_result or {}
    allowed_fields = set(CargoV3Output.model_fields) | set(orig_json)
    unknown_fields = sorted(set(corrected_patch) - allowed_fields)
    if unknown_fields:
        raise HTTPException(
            status_code=422,
            detail=f"纠错 JSON 包含未知字段: {', '.join(unknown_fields[:10])}",
        )
    corr_json = {**orig_json, **corrected_patch}
    encoded_size = len(json.dumps(corr_json, ensure_ascii=False).encode("utf-8"))
    if encoded_size > MAX_FEEDBACK_JSON_BYTES:
        raise HTTPException(status_code=413, detail="纠错 JSON 超出 64 KiB 限制")

    diff_fields = compute_json_diff_fields(orig_json, corr_json)
    if not diff_fields:
        raise HTTPException(status_code=422, detail="纠错内容与原抽取结果一致，请至少修改一个字段")

    # 2. Check if existing feedback exists
    fb_stmt = select(TaskFeedback).where(
        TaskFeedback.task_id == task_id,
        TaskFeedback.tenant_id == tenant.id,
    )
    fb_res = await db.execute(fb_stmt)
    feedback = fb_res.scalars().first()

    if feedback:
        if feedback.status in ["ACCEPTED", "RESOLVED"]:
            raise HTTPException(status_code=400, detail="该任务纠错反馈已被采纳处理，无需重复提交")
        feedback.original_result = orig_json
        feedback.corrected_result = corr_json
        feedback.diff_fields = diff_fields
        feedback.notes = payload.notes
        feedback.status = "PENDING"
        feedback.error_category = "UNSPECIFIED"
        feedback.review_comment = None
        feedback.reviewed_by = None
        feedback.reviewed_at = None
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

    try:
        await db.commit()
    except IntegrityError as exc:
        # SQLite ignores SELECT ... FOR UPDATE and PostgreSQL deployments may
        # still receive a race during rolling upgrades. Reuse the row created
        # by the winning request instead of returning a generic HTTP 500.
        await db.rollback()
        feedback = (
            await db.execute(
                select(TaskFeedback).where(
                    TaskFeedback.task_id == task_id,
                    TaskFeedback.tenant_id == tenant.id,
                )
            )
        ).scalars().first()
        if feedback is None:
            raise HTTPException(status_code=409, detail="反馈提交冲突，请重试") from exc
        if feedback.status in {"ACCEPTED", "RESOLVED"}:
            raise HTTPException(status_code=409, detail="该任务反馈已完成审核") from exc
        feedback.original_result = orig_json
        feedback.corrected_result = corr_json
        feedback.diff_fields = diff_fields
        feedback.notes = payload.notes
        feedback.status = "PENDING"
        feedback.error_category = "UNSPECIFIED"
        feedback.review_comment = None
        feedback.reviewed_by = None
        feedback.reviewed_at = None
        await db.commit()
    await db.refresh(feedback)

    return {
        "code": 0,
        "message": "纠错反馈提交成功；审核确认后，符合原始扣款条件的任务将退款并进入优化流程",
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
    fb_stmt = select(TaskFeedback).where(
        TaskFeedback.task_id == task_id,
        TaskFeedback.tenant_id == tenant.id,
    )
    fb_res = await db.execute(fb_stmt)
    feedback = fb_res.scalars().first()
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
