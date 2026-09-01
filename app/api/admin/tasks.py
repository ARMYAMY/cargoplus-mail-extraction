import json
from pathlib import PureWindowsPath
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.task import EmailTask
from app.models.feedback import TaskFeedback
from app.schemas.task import (
    TaskDetailResponse,
    TaskFeedbackSummary,
    TaskListResponse,
    TaskStatusBatchRequest,
    TaskStatusItem,
)
from app.api.deps import verify_admin_access
from app.services.billing_service import BillingService
from app.services.queue_service import task_queue

router = APIRouter(prefix="/admin/tasks", dependencies=[Depends(verify_admin_access)])


def _attachment_names(file_paths: Optional[str]) -> list[str]:
    try:
        paths = json.loads(file_paths) if isinstance(file_paths, str) else file_paths
    except (TypeError, ValueError):
        return []
    if not isinstance(paths, list):
        return []
    return [
        name
        for raw_path in paths[:20]
        if isinstance(raw_path, str) and (name := PureWindowsPath(raw_path).name)
    ]


def _format_task_response(
    task: EmailTask,
    feedback: Optional[TaskFeedback] = None,
) -> TaskDetailResponse:
    result_data = None
    if task.result_json:
        try:
            result_data = json.loads(task.result_json)
        except Exception:
            result_data = None

    return TaskDetailResponse(
        id=task.id,
        tenant_id=task.tenant_id,
        input_type=task.input_type,
        mail_subject=task.mail_subject,
        status=task.status,
        input_summary=task.input_summary,
        result_json=result_data,
        error_message=task.error_message,
        charged_amount=task.charged_amount,
        is_charged=task.is_charged,
        reserved_amount=task.reserved_amount,
        is_reserved=task.is_reserved,
        duration_ms=task.duration_ms,
        recognition_mode=task.recognition_mode or "standard",
        vision_pages_total=task.vision_pages_total or 0,
        vision_pages_processed=task.vision_pages_processed or 0,
        vision_duration_ms=task.vision_duration_ms,
        callback_url=task.callback_url,
        callback_status=task.callback_status,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        attachment_names=_attachment_names(task.file_paths),
        feedback=(
            TaskFeedbackSummary(
                id=feedback.id,
                status=feedback.status,
                diff_fields_count=len(feedback.diff_fields or []),
                is_refunded=feedback.is_refunded,
                refund_amount=feedback.refund_amount or 0,
                review_comment=feedback.review_comment,
            )
            if feedback
            else None
        ),
    )


@router.get("", response_model=TaskListResponse, summary="管理员全局查询任务列表")
async def list_all_tasks_admin(
    search: Optional[str] = Query(None, description="搜索任务ID、租户ID或主题"),
    status_filter: Optional[str] = Query(None, alias="status"),
    tenant_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(EmailTask)

    if tenant_id:
        query = query.where(EmailTask.tenant_id == tenant_id)
    if status_filter:
        query = query.where(EmailTask.status == status_filter.upper())
    if search:
        s = f"%{search.strip()}%"
        query = query.where(
            or_(
                EmailTask.id.ilike(s),
                EmailTask.tenant_id.ilike(s),
                EmailTask.mail_subject.ilike(s),
            )
        )

    count_stmt = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    query = query.order_by(EmailTask.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    res = await db.execute(query)
    tasks = res.scalars().all()

    return TaskListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[_format_task_response(t) for t in tasks],
    )


@router.post("/statuses", response_model=list[TaskStatusItem], summary="批量查询指定任务状态")
async def get_task_statuses_admin(
    data: TaskStatusBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    # Preserve the request's bounded size while avoiding redundant SQL parameters.
    task_ids = list(dict.fromkeys(data.task_ids))
    result = await db.execute(
        select(EmailTask.id, EmailTask.status).where(EmailTask.id.in_(task_ids))
    )
    statuses = {task_id: task_status for task_id, task_status in result.all()}
    return [
        TaskStatusItem(id=task_id, status=statuses[task_id])
        for task_id in task_ids
        if task_id in statuses
    ]


@router.get("/{task_id}", response_model=TaskDetailResponse, summary="管理员查看单条任务完整上下文")
async def get_task_detail_admin(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    task = (
        await db.execute(select(EmailTask).where(EmailTask.id == task_id))
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    feedback = (
        await db.execute(select(TaskFeedback).where(TaskFeedback.task_id == task_id))
    ).scalar_one_or_none()
    return _format_task_response(task, feedback)


@router.post("/{task_id}/retry", summary="管理员手动重新触发重试任务")
async def retry_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(EmailTask).where(EmailTask.id == task_id)
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != "FAILED" or task.is_charged or task.is_reserved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only uncharged failed tasks can be retried",
        )

    reserved_amount = await BillingService.reserve_for_new_task(db, task.tenant_id)
    if reserved_amount is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Tenant has insufficient available balance for retry",
        )

    task.status = "PENDING"
    task.error_message = None
    task.started_at = None
    task.completed_at = None
    task.duration_ms = None
    task.result_json = None
    task.vision_pages_total = 0
    task.vision_pages_processed = 0
    task.vision_duration_ms = None
    task.callback_status = "PENDING" if task.callback_url else "NONE"
    task.reserved_amount = reserved_amount
    task.is_reserved = True
    task.lease_owner = None
    task.lease_expires_at = None
    task.last_dispatched_at = None
    await db.commit()

    await task_queue.enqueue(task.id, task.tenant_id)
    return {"code": 0, "message": f"Task {task_id} re-enqueued for retry"}
