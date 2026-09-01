import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.task import EmailTask
from app.models.tenant import ApiKey, Tenant
from app.schemas.task import TaskDetailResponse, TaskListResponse
from app.api.deps import get_current_tenant_and_key

router = APIRouter()


def _format_task_response(task: EmailTask) -> TaskDetailResponse:
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
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailResponse,
    summary="查询单个任务状态与抽取结果",
)
async def get_task_detail(
    task_id: str,
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    stmt = select(EmailTask).where(EmailTask.id == task_id, EmailTask.tenant_id == tenant.id)
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": f"Task {task_id} not found"},
        )

    return _format_task_response(task)


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="分页查询租户历史任务列表",
)
async def list_tenant_tasks(
    status_filter: Optional[str] = Query(None, alias="status", description="按状态筛选: PENDING, PROCESSING, SUCCESS, FAILED"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, _ = tenant_info
    query = select(EmailTask).where(EmailTask.tenant_id == tenant.id)

    if status_filter:
        query = query.where(EmailTask.status == status_filter.upper())

    count_stmt = select(func.count()).select_from(query.subquery())
    total_res = await db.execute(count_stmt)
    total = total_res.scalar() or 0

    query = query.order_by(EmailTask.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    res = await db.execute(query)
    tasks = res.scalars().all()

    return TaskListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[_format_task_response(t) for t in tasks],
    )
