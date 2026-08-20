import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import time
from typing import List, Optional
import uuid
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import get_db
from app.models.task import EmailTask
from app.models.tenant import ApiKey, Tenant
from app.schemas.task import (
    ExtractAsyncRequest,
    ExtractSyncRequest,
    SkillV3InputPayload,
    TaskAsyncResponse,
)
from app.api.deps import get_current_tenant_and_key
from app.services.billing_service import BillingService
from app.services.queue_service import task_queue
from app.services.extraction_service import ExtractionService
from app.services.webhook_service import is_safe_webhook_url

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_UPLOAD_EXTENSIONS = {
    ".eml", ".pdf", ".xlsx", ".docx", ".doc",
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff",
    ".txt", ".csv", ".json", ".md",
}


def utc_now():
    return datetime.now(timezone.utc)


async def validate_callback_url(callback_url: Optional[str]) -> None:
    if callback_url and isinstance(callback_url, str) and not await asyncio.to_thread(is_safe_webhook_url, callback_url):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": 42201, "message": "Webhook URL must resolve exclusively to public HTTP(S) addresses"},
        )


async def reserve_or_raise(db: AsyncSession, tenant_id: str) -> Decimal:
    reserved_amount = await BillingService.reserve_for_new_task(db, tenant_id)
    if reserved_amount is not None:
        return reserved_amount
    _, available_balance, unit_price = await BillingService.check_balance_available(db, tenant_id)
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": 40201,
            "message": "账户可用余额不足，请充值或等待在途任务完成后重试",
            "current_balance": float(available_balance),
            "unit_price": float(unit_price),
        },
    )


def normalize_idempotency_key(value: Optional[str]) -> Optional[str]:
    if value is None or not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": 42203, "message": "Idempotency-Key must contain 1-128 printable characters"},
        )
    return normalized


async def find_idempotent_task(
    db: AsyncSession,
    tenant_id: str,
    idempotency_key: Optional[str],
) -> Optional[EmailTask]:
    if not idempotency_key:
        return None
    return (
        await db.execute(
            select(EmailTask).where(
                EmailTask.tenant_id == tenant_id,
                EmailTask.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def enforce_queue_capacity(db: AsyncSession, tenant_id: str) -> None:
    active_statuses = ["PENDING", "PROCESSING"]
    tenant_pending = (
        await db.execute(
            select(func.count(EmailTask.id)).where(
                EmailTask.tenant_id == tenant_id,
                EmailTask.status.in_(active_statuses),
            )
        )
    ).scalar_one()
    if tenant_pending >= settings.MAX_TENANT_PENDING_TASKS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": 42901, "message": "Tenant pending-task limit reached"},
            headers={"Retry-After": "30"},
        )
    global_pending = (
        await db.execute(
            select(func.count(EmailTask.id)).where(EmailTask.status.in_(active_statuses))
        )
    ).scalar_one()
    if global_pending >= settings.MAX_GLOBAL_PENDING_TASKS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": 42902, "message": "Platform pending-task limit reached"},
            headers={"Retry-After": "30"},
        )


def async_task_response(task: EmailTask, *, duplicate: bool = False) -> TaskAsyncResponse:
    return TaskAsyncResponse(
        code=0,
        message="Existing idempotent task returned" if duplicate else "Task submitted successfully",
        task_id=task.id,
        status=task.status,
        created_at=task.created_at,
    )


@router.post(
    "/extract/async",
    response_model=TaskAsyncResponse,
    summary="异步提交邮件抽取任务 (JSON 文本模式)",
    description="提交结构化邮件正文与附件文本，立即返回 task_id 并进入异步处理队列。处理完成后通过 Webhook 回调推送结果或通过 /tasks/{id} 查询。",
)
async def extract_async_json(
    request: ExtractAsyncRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, api_key = tenant_info
    idempotency_key = normalize_idempotency_key(idempotency_key)
    existing_task = await find_idempotent_task(db, tenant.id, idempotency_key)
    if existing_task:
        return async_task_response(existing_task, duplicate=True)
    await validate_callback_url(request.callback_url)
    await enforce_queue_capacity(db, tenant.id)

    reserved_amount = await reserve_or_raise(db, tenant.id)

    # Build input payload
    payload = SkillV3InputPayload(
        mail_subject=request.mail_subject or "",
        mail_body=request.mail_body or "",
        attachments=request.attachments or [],
    )

    task = EmailTask(
        tenant_id=tenant.id,
        api_key_id=api_key.id if not api_key.id.startswith("admin_virtual_key_") else None,
        idempotency_key=idempotency_key,
        input_type="JSON",
        mail_subject=request.mail_subject or "无主题",
        status="PENDING",
        input_summary=f"正文长度: {len(request.mail_body or '')} 字符, 附件数: {len(request.attachments or [])}",
        raw_input_json=json.dumps(payload.model_dump(), ensure_ascii=False),
        callback_url=request.callback_url,
        callback_status="PENDING" if request.callback_url else "NONE",
        reserved_amount=reserved_amount,
        is_reserved=True,
    )
    db.add(task)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing_task = await find_idempotent_task(db, tenant.id, idempotency_key)
        if existing_task:
            return async_task_response(existing_task, duplicate=True)
        raise
    await db.refresh(task)

    # Enqueue task for background worker
    try:
        await task_queue.enqueue(task.id, tenant.id)
    except Exception as exc:
        logger.error("Failed to dispatch durable task %s: %s", task.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": 50301,
                "message": "Task was saved but the queue is temporarily unavailable",
                "task_id": task.id,
            },
            headers={"Retry-After": "10"},
        ) from exc

    return async_task_response(task)


@router.post(
    "/extract/async/upload",
    response_model=TaskAsyncResponse,
    summary="异步提交邮件抽取任务 (原始文件上传模式)",
    description="支持直接上传 .eml 邮件、.pdf/.xlsx/.docx/.doc 附件及图片文件，由系统自动解析并进行 V3 字段抽取。",
)
async def extract_async_upload(
    files: List[UploadFile] = File(..., description="上传的文件列表（支持 .eml, .pdf, .xlsx, .docx, .doc, 图片等）"),
    mail_subject: Optional[str] = Form("", description="可选补充的邮件主题"),
    callback_url: Optional[str] = Form(None, description="Webhook 回调地址"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, api_key = tenant_info
    tenant_id = tenant.id
    api_key_id = api_key.id if not api_key.id.startswith("admin_virtual_key_") else None
    idempotency_key = normalize_idempotency_key(idempotency_key)
    existing_task = await find_idempotent_task(db, tenant_id, idempotency_key)
    if existing_task:
        for upload in files:
            await upload.close()
        return async_task_response(existing_task, duplicate=True)
    await validate_callback_url(callback_url)
    await enforce_queue_capacity(db, tenant_id)
    subject_str = mail_subject if isinstance(mail_subject, str) else ""
    if subject_str and len(subject_str) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": 42202, "message": "mail_subject exceeds 255 characters"},
        )

    # Fast rejection before accepting potentially large request bodies. A second,
    # atomic reservation is performed after the upload is safely stored.
    is_sufficient, balance, unit_price = await BillingService.check_balance_available(db, tenant.id)
    if not is_sufficient:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": 40201,
                "message": "账户余额不足，请充值后重试",
                "current_balance": float(balance),
                "unit_price": float(unit_price),
            },
        )
    await db.rollback()

    # Save uploaded files to storage directory
    if len(files) > settings.MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"code": 41302, "message": f"一次最多上传 {settings.MAX_UPLOAD_FILES} 个文件"},
        )

    saved_file_paths: List[str] = []
    task_id_prefix = uuid.uuid4().hex[:8]

    import re
    for upload in files:
        raw_name = Path(upload.filename or "file").name
        if Path(raw_name).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"code": 41501, "message": f"不支持的文件类型: {raw_name}"},
            )

    total_size = 0
    try:
        upload_root = settings.uploads_path.resolve()
        for upload in files:
            raw_name = Path(upload.filename or "file").name
            extension = Path(raw_name).suffix.lower()
            file_limit = (
                settings.MAX_LEGACY_DOC_FILE_SIZE
                if extension == ".doc"
                else settings.MAX_UPLOAD_FILE_SIZE
            )
            safe_base_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw_name)[:180] or "file"
            safe_filename = f"{task_id_prefix}_{uuid.uuid4().hex[:12]}_{safe_base_name}"
            dest_path = (upload_root / safe_filename).resolve()
            if dest_path.parent != upload_root:
                raise HTTPException(status_code=400, detail={"code": 40003, "message": "Invalid filename"})

            saved_file_paths.append(str(dest_path))
            file_size = 0
            try:
                with dest_path.open("xb") as output:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        file_size += len(chunk)
                        total_size += len(chunk)
                        if file_size > file_limit:
                            raise HTTPException(
                                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                detail={
                                    "code": 41301,
                                    "message": f"单文件 {raw_name} 超过最大允许大小 {file_limit // (1024 * 1024)}MB",
                                },
                            )
                        if total_size > settings.MAX_UPLOAD_TOTAL_SIZE:
                            raise HTTPException(
                                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                detail={"code": 41303, "message": "上传文件总大小超过限制"},
                            )
                        output.write(chunk)
            finally:
                await upload.close()
    except Exception:
        for upload in files:
            try:
                await upload.close()
            except OSError:
                pass
        for saved_path in saved_file_paths:
            try:
                Path(saved_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove partial upload %s", saved_path)
        raise

    summary = f"上传文件数: {len(saved_file_paths)} ({', '.join(Path(f.filename or 'file').name for f in files)})"


    task = EmailTask(
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        idempotency_key=idempotency_key,
        input_type="FILE",
        mail_subject=mail_subject or (files[0].filename if files else "文件上传"),
        status="PENDING",
        input_summary=summary,
        file_paths=json.dumps(saved_file_paths, ensure_ascii=False),
        callback_url=callback_url,
        callback_status="PENDING" if callback_url else "NONE",
    )
    try:
        reserved_amount = await reserve_or_raise(db, tenant_id)
        task.reserved_amount = reserved_amount
        task.is_reserved = True
        db.add(task)
        await db.commit()
        await db.refresh(task)
    except IntegrityError:
        await db.rollback()
        for saved_path in saved_file_paths:
            try:
                Path(saved_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove duplicate upload %s", saved_path)
        existing_task = await find_idempotent_task(db, tenant_id, idempotency_key)
        if existing_task:
            return async_task_response(existing_task, duplicate=True)
        raise
    except Exception:
        await db.rollback()
        for saved_path in saved_file_paths:
            try:
                Path(saved_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove upload after task creation error: %s", saved_path)
        raise

    # Enqueue task
    try:
        await task_queue.enqueue(task.id, tenant_id)
    except Exception as exc:
        logger.error("Failed to dispatch durable upload task %s: %s", task.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": 50301,
                "message": "Task was saved but the queue is temporarily unavailable",
                "task_id": task.id,
            },
            headers={"Retry-After": "10"},
        ) from exc

    return async_task_response(task)


@router.post(
    "/extract/sync",
    summary="同步邮件抽取接口 (轻量调试/即时调用)",
    description="同步等待抽取与归一化完成并直接返回 V3 业务 JSON。适合轻量请求或联调测试。",
)
async def extract_sync(
    request: ExtractSyncRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_info: tuple[Tenant, ApiKey] = Depends(get_current_tenant_and_key),
    db: AsyncSession = Depends(get_db),
):
    tenant, api_key = tenant_info
    idempotency_key = normalize_idempotency_key(idempotency_key)
    task = await find_idempotent_task(db, tenant.id, idempotency_key)
    if task is None:
        await enforce_queue_capacity(db, tenant.id)
        reserved_amount = await reserve_or_raise(db, tenant.id)

        payload = SkillV3InputPayload(
            mail_subject=request.mail_subject or "",
            mail_body=request.mail_body or "",
            attachments=request.attachments or [],
        )

        task = EmailTask(
            tenant_id=tenant.id,
            api_key_id=api_key.id if not api_key.id.startswith("admin_virtual_key_") else None,
            idempotency_key=idempotency_key,
            input_type="JSON",
            mail_subject=request.mail_subject or "同步调用",
            status="PENDING",
            input_summary=f"正文长度: {len(request.mail_body or '')}, 附件数: {len(request.attachments or [])}",
            raw_input_json=json.dumps(payload.model_dump(), ensure_ascii=False),
            reserved_amount=reserved_amount,
            is_reserved=True,
        )
        db.add(task)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            task = await find_idempotent_task(db, tenant.id, idempotency_key)
            if task is None:
                raise
        else:
            await db.refresh(task)

    submitted_task_id = task.id
    if task.status in {"PENDING", "PROCESSING"}:
        if settings.TASK_QUEUE_MODE == "celery":
            if task.status == "PENDING":
                try:
                    await task_queue.enqueue(submitted_task_id, tenant.id)
                except Exception as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"code": 50301, "message": "Task queue is unavailable", "task_id": task.id},
                        headers={"Retry-After": "10"},
                    ) from exc

            deadline = time.monotonic() + settings.TASK_TIMEOUT_SECONDS + 30
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                await db.rollback()
                current_status = (
                    await db.execute(select(EmailTask.status).where(EmailTask.id == submitted_task_id))
                ).scalar_one_or_none()
                if current_status in {"SUCCESS", "FAILED"}:
                    break
            else:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail={
                        "code": 50401,
                        "message": "Synchronous wait timed out; query the task endpoint for its final status",
                        "task_id": submitted_task_id,
                    },
                )
        else:
            await ExtractionService.process_task(submitted_task_id, api_key.api_secret)

    # End the request session's earlier read transaction before observing the worker
    # session's commit (important for SQLite snapshot semantics).
    await db.rollback()
    updated_task = (
        await db.execute(
            select(EmailTask)
            .where(EmailTask.id == submitted_task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    if updated_task.status == "SUCCESS":

        result_data = json.loads(updated_task.result_json or "{}")
        return {
            "code": 0,
            "message": "Success",
            "task_id": updated_task.id,
            "status": "SUCCESS",
            "duration_ms": updated_task.duration_ms,
            "charged_amount": float(updated_task.charged_amount),
            "model_used": getattr(updated_task, "model_used", None) or settings.LLM_MODEL,
            "data": result_data,
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": 50001,
                "message": "Extraction failed",
                "task_id": updated_task.id,
                "status": updated_task.status,
                "error": updated_task.error_message,
            },
        )
