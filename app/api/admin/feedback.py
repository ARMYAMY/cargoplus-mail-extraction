import json
import logging
import hashlib
import shutil
import asyncio
import httpx
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import AsyncSessionLocal, get_db
from app.api.deps import verify_admin_access
from app.models.feedback import (
    AdminJob, BenchmarkCase, BenchmarkRevision, EvaluationRun, FewShotExample, PromptVersion,
    SystemVersion, TaskFeedback,
)
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.schemas.feedback import (
    FewShotCreateRequest,
    FewShotUpdateRequest,
    SystemVersionReleaseRequest,
    TaskFeedbackReviewRequest,
    PromptCreateRequest,
    PromptOptimizeRequest,
    PromptRefineRequest,
    PromptFinalizeRequest,
    BenchmarkUpdateRequest,
)
from app.services.billing_service import BillingService
from app.services.evaluation_service import EvaluationService, build_ab_comparison, build_field_diff_rows
from app.services.few_shot_service import FewShotService
from app.services.prompt_service import PromptService
from app.core.skill_runner import default_skill_runner
from app.services.admin_job_service import AdminJobService, JobCancelled, job_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin_access)], tags=["Admin Feedback & Optimization"])


def utc_now():
    return datetime.now(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _safe_attachment_names(value: Any) -> List[str]:
    paths = _parse_json_value(value, [])
    if not isinstance(paths, list):
        return []
    names: List[str] = []
    for raw_path in paths[:100]:
        if not isinstance(raw_path, str):
            continue
        name = _safe_attachment_name(raw_path)
        if name:
            names.append(name)
    return names


def _safe_attachment_name(raw_path: str) -> str:
    """Return a basename consistently for paths persisted on any operating system."""
    return PureWindowsPath(raw_path).name


# ==========================================
# 1. Feedbacks Management & Audit
# ==========================================

@router.get("/feedbacks", summary="管理端分页获取纠错反馈列表")
async def list_feedbacks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status: Optional[str] = Query(
        None,
        pattern="^(PENDING|ACCEPTED|REJECTED|RESOLVED)$",
        description="状态过滤: PENDING, ACCEPTED, REJECTED, RESOLVED",
    ),
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
        select(
            TaskFeedback,
            Tenant.name.label("tenant_name"),
            EmailTask.mail_subject,
            EmailTask.input_type,
            EmailTask.created_at.label("task_time"),
            EmailTask.input_summary,
            EmailTask.raw_input_json,
            EmailTask.file_paths,
            EmailTask.is_charged,
            EmailTask.charged_amount,
        )
        .outerjoin(Tenant, TaskFeedback.tenant_id == Tenant.id)
        .outerjoin(EmailTask, TaskFeedback.task_id == EmailTask.id)
        .where(TaskFeedback.id == feedback_id)
    )
    res = await db.execute(stmt)
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="工单不存在")

    (
        fb,
        t_name,
        subject,
        input_type,
        task_time,
        input_summary,
        raw_input_json,
        file_paths,
        is_charged,
        charged_amount,
    ) = row

    parsed_raw_input = _parse_json_value(raw_input_json, raw_input_json)
    attachment_names = _safe_attachment_names(file_paths)

    return {
        "code": 0,
        "data": {
            "id": fb.id,
            "task_id": fb.task_id,
            "task_subject": subject or "-",
            "input_type": input_type or "-",
            "task_time": task_time.isoformat() if task_time else "-",
            "input_summary": input_summary or "",
            "raw_input_json": parsed_raw_input,
            # Never expose server-local absolute storage paths in the browser.
            "file_paths": attachment_names,
            "is_charged": bool(is_charged),
            "charged_amount": float(charged_amount or 0),
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


@router.get("/feedbacks/{feedback_id}/attachments/{filename}", summary="下载反馈工单附件")
async def download_feedback_attachment(
    feedback_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    # The route only accepts a basename.  The actual path is always recovered from
    # the feedback's task record, never from a user-controlled path fragment.
    if not filename or _safe_attachment_name(filename) != filename:
        raise HTTPException(status_code=404, detail="附件不存在")

    stmt = select(TaskFeedback.task_id).where(TaskFeedback.id == feedback_id)
    res = await db.execute(stmt)
    task_id = res.scalar_one_or_none()
    if not task_id:
        raise HTTPException(status_code=404, detail="工单不存在")

    task_stmt = select(EmailTask.file_paths).where(EmailTask.id == task_id)
    task_res = await db.execute(task_stmt)
    file_paths = task_res.scalar_one_or_none() or []
    if isinstance(file_paths, str):
        file_paths = _parse_json_value(file_paths, [])
    if not isinstance(file_paths, list):
        raise HTTPException(status_code=404, detail="附件不存在")

    uploads_root = settings.uploads_path.resolve()
    for raw_path in file_paths:
        if not isinstance(raw_path, str):
            continue
        stored_name = _safe_attachment_name(raw_path)
        if stored_name != filename:
            continue

        # Prefer the persisted path.  The uploads-root fallback keeps attachments
        # downloadable after moving a database between Windows/Linux containers,
        # where the old absolute prefix is no longer meaningful.
        candidates = [Path(raw_path)]
        stored_parent_name = PureWindowsPath(raw_path).parent.name
        if stored_parent_name.casefold() == uploads_root.name.casefold():
            candidates.append(uploads_root / stored_name)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(uploads_root) and resolved.is_file():
                return FileResponse(
                    path=str(resolved),
                    filename=stored_name,
                    media_type="application/octet-stream",
                )

        logger.warning(
            "Feedback attachment record points to a missing file: feedback_id=%s task_id=%s filename=%s",
            feedback_id,
            task_id,
            stored_name,
        )

    raise HTTPException(status_code=404, detail="附件不存在")


@router.post("/feedbacks/{feedback_id}/accept", summary="审核采纳反馈 (自动退费冲正 + 自动生成评测用例/Few-Shot)")
async def accept_feedback(
    feedback_id: str,
    payload: TaskFeedbackReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    if payload.status != "ACCEPTED":
        raise HTTPException(status_code=422, detail="采纳接口的 status 必须为 ACCEPTED")

    stmt = (
        select(TaskFeedback)
        .where(TaskFeedback.id == feedback_id)
        .with_for_update()
    )
    res = await db.execute(stmt)
    fb = res.scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="工单不存在")

    if fb.status in ["ACCEPTED", "RESOLVED"]:
        return {"code": 0, "message": "该工单此前已被采纳", "data": {"status": fb.status}}
    if fb.status != "PENDING":
        raise HTTPException(status_code=409, detail=f"工单当前状态为 {fb.status}，不能采纳")
    if not fb.diff_fields:
        raise HTTPException(status_code=409, detail="反馈没有有效字段差异，不能采纳或退款")

    task_stmt = select(EmailTask).where(EmailTask.id == fb.task_id)
    task = (await db.execute(task_stmt)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=409, detail="关联抽取任务已不存在，无法审核")

    # 1. Financial Auto-Refund
    refund_tx = None
    if payload.auto_refund and not fb.is_refunded:
        try:
            refund_tx = await BillingService.refund_task_deduction(
                db=db,
                tenant_id=fb.tenant_id,
                task_id=fb.task_id,
                operator="ADMIN",
                reason=f"采纳反馈 [{feedback_id}] {payload.review_comment or ''}",
            )
        except (RuntimeError, ValueError) as exc:
            await db.rollback()
            logger.error("Feedback %s refund aborted: %s", feedback_id, exc)
            raise HTTPException(status_code=409, detail="退款校验失败，请先核对原始扣款流水") from exc
        if refund_tx:
            fb.is_refunded = True
            fb.refund_amount = refund_tx.amount
            fb.refund_tx_id = refund_tx.id

    fb.status = "ACCEPTED"
    fb.error_category = payload.error_category or "UNSPECIFIED"
    fb.document_type = payload.document_type or "GENERAL"
    fb.review_comment = payload.review_comment or "审核通过并采纳"
    fb.reviewed_by = "admin"
    fb.reviewed_at = utc_now()

    # 2. Convert to BenchmarkCase
    if payload.create_benchmark and fb.diff_fields and fb.corrected_result:
        benchmark_dir = (settings.uploads_path.parent / "benchmark_files" / fb.id).resolve()
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        copied_files = []
        file_hashes = {}
        stored_paths = _parse_json_value(task.file_paths, [])
        if isinstance(stored_paths, list):
            uploads_root = settings.uploads_path.resolve()
            for raw_path in stored_paths:
                if not isinstance(raw_path, str):
                    continue
                source = Path(raw_path).resolve()
                if not source.is_file() or not source.is_relative_to(uploads_root):
                    continue
                safe_name = _safe_attachment_name(str(source))
                destination = benchmark_dir / safe_name
                shutil.copy2(source, destination)
                digest = _sha256_file(destination)
                copied_files.append(str(destination))
                file_hashes[safe_name] = digest
        # Only fields explicitly corrected by the customer and reviewed by the
        # administrator are eligible ground truth. Other values originated from
        # the model itself and must never silently become gold labels.
        reviewed_truth = {
            field: (fb.corrected_result or {}).get(field)
            for field in (fb.diff_fields or [])
            if field in (fb.corrected_result or {})
        }
        bm = BenchmarkCase(
            feedback_id=fb.id,
            doc_type=payload.document_type or "GENERAL",
            title=f"纠错金标用例 (Task: {fb.task_id})",
            input_text=((task.mail_subject or "") + "\n" + (task.input_summary or "")).strip(),
            raw_file_path=copied_files[0] if copied_files else None,
            source_files=copied_files,
            source_hashes=file_hashes,
            ground_truth=reviewed_truth,
            is_active=False,
            verification_status="DRAFT",
        )
        db.add(bm)
        await db.flush()
        fb.is_benchmark = True
        fb.benchmark_id = bm.id

    # 3. Convert to FewShot Example
    if payload.create_few_shot and fb.diff_fields and fb.corrected_result:
        input_sample = ((task.mail_subject or "") + "\n" + (task.input_summary or "")).strip()
        if not input_sample:
            input_sample = "标准单证输入"
        fs = FewShotExample(
            feedback_id=fb.id,
            source_tenant_id=fb.tenant_id,
            doc_type=payload.document_type or "GENERAL",
            error_category=payload.error_category or "UNSPECIFIED",
            lifecycle_status="DRAFT",
            title=f"纠错样例: {fb.diff_fields[0] if fb.diff_fields else '海运字段纠错'}",
            input_excerpt=input_sample[:1000],
            expected_output=fb.corrected_result or {},
            is_active=False,
            priority=20,
        )
        db.add(fs)
        FewShotService.invalidate_cache()

    try:
        await db.commit()
        await db.refresh(fb)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="反馈已被其他审核请求处理，请刷新后重试") from exc

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
    if payload.status != "REJECTED":
        raise HTTPException(status_code=422, detail="驳回接口的 status 必须为 REJECTED")

    stmt = (
        select(TaskFeedback)
        .where(TaskFeedback.id == feedback_id)
        .with_for_update()
    )
    res = await db.execute(stmt)
    fb = res.scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="工单不存在")

    if fb.status != "PENDING":
        return {"code": 0, "message": f"该工单已审核处理 ({fb.status})，无需重复操作", "data": {"status": fb.status}}

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
                "lifecycle_status": it.lifecycle_status,
                "error_category": it.error_category,
                "evaluation_run_id": it.evaluation_run_id,
                "parent_id": it.parent_id,
                "feedback_id": it.feedback_id,
                "source_tenant_id": it.source_tenant_id,
                "priority": it.priority,
                "created_at": it.created_at.isoformat(),
                "updated_at": it.updated_at.isoformat(),
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
        is_active=False,
        lifecycle_status="DRAFT",
    )
    db.add(fs)
    await db.commit()
    await db.refresh(fs)
    FewShotService.invalidate_cache()
    return {"code": 0, "message": "Few-Shot 候选已保存，请回归验证后再启用", "data": {"id": fs.id}}


@router.get("/few-shots/{fs_id}", summary="获取 Few-Shot 完整详情")
async def get_few_shot(fs_id: str, db: AsyncSession = Depends(get_db)):
    fs = (await db.execute(select(FewShotExample).where(FewShotExample.id == fs_id))).scalar_one_or_none()
    if not fs:
        raise HTTPException(status_code=404, detail="示例不存在")
    tenant_name = None
    if fs.source_tenant_id:
        tenant_name = (
            await db.execute(select(Tenant.name).where(Tenant.id == fs.source_tenant_id))
        ).scalar_one_or_none()
    return {"code": 0, "data": {
        "id": fs.id,
        "feedback_id": fs.feedback_id,
        "parent_id": fs.parent_id,
        "source_tenant_id": fs.source_tenant_id,
        "tenant_name": tenant_name,
        "scope": "TENANT" if fs.source_tenant_id else "GLOBAL",
        "doc_type": fs.doc_type,
        "error_category": fs.error_category,
        "title": fs.title,
        "input_excerpt": fs.input_excerpt,
        "expected_output": fs.expected_output,
        "priority": fs.priority,
        "is_active": fs.is_active,
        "lifecycle_status": fs.lifecycle_status,
        "evaluation_run_id": fs.evaluation_run_id,
        "created_at": fs.created_at.isoformat(),
        "updated_at": fs.updated_at.isoformat(),
    }}


@router.put("/few-shots/{fs_id}", summary="修改 Few-Shot 示例")
async def update_few_shot(fs_id: str, payload: FewShotUpdateRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(FewShotExample).where(FewShotExample.id == fs_id)
    res = await db.execute(stmt)
    fs = res.scalar_one_or_none()
    if not fs:
        raise HTTPException(status_code=404, detail="示例不存在")

    content_changed = any(value is not None for value in (
        payload.doc_type, payload.title, payload.input_excerpt,
        payload.expected_output, payload.priority,
    ))
    if content_changed and fs.is_active:
        candidate = FewShotExample(
            parent_id=fs.id,
            source_tenant_id=fs.source_tenant_id,
            doc_type=payload.doc_type or fs.doc_type,
            error_category=fs.error_category,
            title=payload.title or fs.title,
            input_excerpt=payload.input_excerpt or fs.input_excerpt,
            expected_output=payload.expected_output or fs.expected_output,
            priority=payload.priority or fs.priority,
            is_active=False,
            lifecycle_status="DRAFT",
        )
        db.add(candidate)
        await db.commit()
        await db.refresh(candidate)
        return {
            "code": 0,
            "message": "已生效示例不会被直接覆盖；修改内容已保存为候选版本，请回归后启用",
            "data": {"id": candidate.id, "parent_id": fs.id},
        }

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
        if payload.is_active and fs.lifecycle_status not in {"VALIDATED", "ACTIVE"}:
            raise HTTPException(status_code=409, detail="候选 Few-Shot 必须先通过金标回归，不能直接启用")
        fs.is_active = payload.is_active
        if payload.is_active:
            fs.lifecycle_status = "ACTIVE"

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


@router.post("/few-shots/{fs_id}/evaluate", summary="验证候选 Few-Shot")
async def evaluate_few_shot(fs_id: str, db: AsyncSession = Depends(get_db)):
    fs = (await db.execute(select(FewShotExample).where(FewShotExample.id == fs_id))).scalar_one_or_none()
    if not fs:
        raise HTTPException(status_code=404, detail="示例不存在")
    snippet = (
        "\n### 待验证的纠错参考案例（仅本次回归使用）\n"
        f"输入片段：\n{fs.input_excerpt}\n"
        f"期望输出：\n{json.dumps(fs.expected_output, ensure_ascii=False, indent=2)}\n"
    )
    result = await _run_layered_evaluation(
        db,
        extra_few_shot_snippet=snippet,
        exclude_feedback_id=fs.feedback_id,
    )
    fs.evaluation_run_id = result.get("evaluation_run_id")
    fs.lifecycle_status = "VALIDATED" if result.get("can_release") else "FAILED"
    fs.is_active = False
    await db.commit()
    return {"code": 0, "message": "Few-Shot 候选回归完成", "data": result}


# ==========================================
# 3. Automated Benchmark Regression Evaluation
# ==========================================

def _holdout_summary(result: dict) -> dict:
    """Return release metrics without exposing holdout documents or answers."""
    return {
        "dataset_role": "HOLDOUT",
        "total_cases": int(result.get("total_cases") or 0),
        "passed_cases": int(result.get("passed_cases") or 0),
        "failed_cases": int(result.get("failed_cases") or 0),
        "overall_accuracy_percent": float(result.get("overall_accuracy_percent") or 0),
        "critical_regressions_count": int(result.get("critical_regressions_count") or 0),
        "critical_failure_cases_count": int(result.get("critical_failure_cases_count") or 0),
        "duration_seconds": float(result.get("duration_seconds") or 0),
        "can_release": bool(result.get("can_release")),
        "evaluation_run_id": result.get("evaluation_run_id"),
    }


def _layered_evaluation_result(training: dict, holdout: dict) -> dict:
    train_total = int(training.get("total_cases") or 0)
    holdout_total = int(holdout.get("total_cases") or 0)
    total = train_total + holdout_total
    weighted_accuracy = (
        float(training.get("overall_accuracy_percent") or 0) * train_total
        + float(holdout.get("overall_accuracy_percent") or 0) * holdout_total
    )
    overall = round(weighted_accuracy / total, 1) if total else 0.0
    checks = [
        {"code": "TRAIN_PRESENT", "label": "优化集已配置", "passed": train_total > 0,
         "detail": f"优化集已启用 {train_total} 个案例" if train_total else "优化集没有已确认且启用的案例"},
        {"code": "TRAIN_PASS", "label": "优化集回归通过", "passed": train_total > 0 and bool(training.get("can_release")),
         "detail": f"优化集准确率 {float(training.get('overall_accuracy_percent') or 0):.1f}%"},
        {"code": "HOLDOUT_PRESENT", "label": "保密测试集已配置", "passed": holdout_total > 0,
         "detail": f"保密测试集已启用 {holdout_total} 个案例" if holdout_total else "请至少设置一个保密测试案例"},
        {"code": "HOLDOUT_PASS", "label": "保密测试集门禁通过", "passed": holdout_total > 0 and bool(holdout.get("can_release")),
         "detail": f"保密测试集准确率 {float(holdout.get('overall_accuracy_percent') or 0):.1f}%，具体答案不向 AI 暴露"},
    ]
    failed = [item for item in checks if not item["passed"]]
    return {
        "dataset_mode": "LAYERED",
        "total_cases": total,
        "passed_cases": int(training.get("passed_cases") or 0) + int(holdout.get("passed_cases") or 0),
        "failed_cases": int(training.get("failed_cases") or 0) + int(holdout.get("failed_cases") or 0),
        "overall_accuracy_percent": overall,
        "duration_seconds": round(float(training.get("duration_seconds") or 0) + float(holdout.get("duration_seconds") or 0), 2),
        "critical_regressions_count": int(training.get("critical_regressions_count") or 0) + int(holdout.get("critical_regressions_count") or 0),
        "critical_failure_cases_count": int(training.get("critical_failure_cases_count") or 0) + int(holdout.get("critical_failure_cases_count") or 0),
        "can_release": not failed,
        "gate_checks": checks,
        "gate_reasons": [item["detail"] for item in failed],
        "training": training,
        "holdout": _holdout_summary(holdout),
        # Only optimization-set details are allowed to leave the evaluation service.
        "case_results": training.get("case_results") or [],
        "field_accuracies": training.get("field_accuracies") or {},
    }


async def _run_layered_evaluation(db: AsyncSession, **kwargs) -> dict:
    training = await EvaluationService.run_benchmark_evaluation(db, dataset_role="TRAIN", **kwargs)
    holdout = await EvaluationService.run_benchmark_evaluation(db, dataset_role="HOLDOUT", **kwargs)
    return _layered_evaluation_result(training, holdout)

@router.get("/benchmarks", summary="金标评测集列表")
async def list_benchmarks(db: AsyncSession = Depends(get_db)):
    cases = (
        await db.execute(select(BenchmarkCase).order_by(BenchmarkCase.created_at.desc()))
    ).scalars().all()
    runs = (
        await db.execute(select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(100))
    ).scalars().all()
    latest_by_case = {}
    for run in runs:
        for result in run.case_results or []:
            latest_by_case.setdefault(result.get("case_id"), {
                "run_id": run.id,
                "started_at": run.started_at.isoformat(),
                **result,
            })
    data = [{
        "id": case.id,
        "feedback_id": case.feedback_id,
        "title": case.title,
        "doc_type": case.doc_type,
        "dataset_role": case.dataset_role or "TRAIN",
        "input_text": case.input_text or "",
        "source_files": [Path(path).name for path in (case.source_files or [])],
        "source_hashes": case.source_hashes or {},
        "ground_truth": case.ground_truth or {},
        "weight": case.weight,
        "is_active": case.is_active,
        "verification_status": case.verification_status or "VERIFIED",
        "verified_by": case.verified_by,
        "verified_at": case.verified_at.isoformat() if case.verified_at else None,
        "latest_result": latest_by_case.get(case.id),
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
    } for case in cases]
    return {"code": 0, "data": data, "meta": {
        "training_count": sum(1 for case in cases if (case.dataset_role or "TRAIN") == "TRAIN"),
        "holdout_count": sum(1 for case in cases if case.dataset_role == "HOLDOUT"),
    }}


@router.get("/benchmarks/{case_id}", summary="金标评测集详情")
async def get_benchmark(case_id: str, db: AsyncSession = Depends(get_db)):
    cases = await list_benchmarks(db)
    item = next((row for row in cases["data"] if row["id"] == case_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="金标案例不存在")
    return {"code": 0, "data": item}


@router.put("/benchmarks/{case_id}", summary="编辑金标元数据和标准答案")
async def update_benchmark(
    case_id: str,
    payload: BenchmarkUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    case = (await db.execute(select(BenchmarkCase).where(BenchmarkCase.id == case_id))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="金标案例不存在")
    db.add(BenchmarkRevision(
        benchmark_id=case.id,
        snapshot={
            "title": case.title,
            "doc_type": case.doc_type,
            "dataset_role": case.dataset_role or "TRAIN",
            "ground_truth": case.ground_truth,
            "weight": case.weight,
            "is_active": case.is_active,
            "verification_status": case.verification_status,
            "verified_by": case.verified_by,
            "verified_at": case.verified_at.isoformat() if case.verified_at else None,
            "updated_at": case.updated_at.isoformat(),
        },
    ))
    if payload.title is not None:
        case.title = payload.title
    if payload.doc_type is not None:
        case.doc_type = payload.doc_type
    if payload.dataset_role is not None:
        case.dataset_role = payload.dataset_role
    if payload.ground_truth is not None:
        case.ground_truth = payload.ground_truth
    if payload.weight is not None:
        case.weight = payload.weight
    if payload.is_active is not None:
        if payload.is_active and case.verification_status != "VERIFIED":
            raise HTTPException(status_code=409, detail="金标必须先完成人工确认才能启用")
        case.is_active = payload.is_active
    if payload.verification_status is not None:
        case.verification_status = payload.verification_status
        if payload.verification_status == "VERIFIED":
            if not case.ground_truth:
                raise HTTPException(status_code=409, detail="标准答案为空，不能确认金标")
            case.verified_by = "admin"
            case.verified_at = utc_now()
            case.is_active = True
        else:
            case.verified_by = None
            case.verified_at = None
            case.is_active = False
    await db.commit()
    return {"code": 0, "message": "金标案例已更新；修改后请重新运行回归"}


@router.get("/benchmarks/{case_id}/revisions", summary="金标修改历史")
async def list_benchmark_revisions(case_id: str, db: AsyncSession = Depends(get_db)):
    items = (
        await db.execute(
            select(BenchmarkRevision)
            .where(BenchmarkRevision.benchmark_id == case_id)
            .order_by(BenchmarkRevision.created_at.desc())
        )
    ).scalars().all()
    return {"code": 0, "data": [{
        "id": item.id,
        "snapshot": item.snapshot,
        "changed_by": item.changed_by,
        "created_at": item.created_at.isoformat(),
    } for item in items]}


@router.get("/benchmarks/{case_id}/files/{filename}", summary="下载金标原始文件")
async def download_benchmark_file(case_id: str, filename: str, db: AsyncSession = Depends(get_db)):
    if not filename or _safe_attachment_name(filename) != filename:
        raise HTTPException(status_code=404, detail="文件不存在")
    case = (await db.execute(select(BenchmarkCase).where(BenchmarkCase.id == case_id))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="金标案例不存在")
    benchmark_root = (settings.uploads_path.parent / "benchmark_files").resolve()
    for raw_path in case.source_files or []:
        candidate = Path(raw_path).resolve()
        if candidate.name == filename and candidate.is_relative_to(benchmark_root) and candidate.is_file():
            return FileResponse(str(candidate), filename=filename, media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="文件不存在")


@router.post("/benchmarks/{case_id}/evaluate", summary="单案例回归测试")
async def evaluate_single_benchmark(case_id: str, db: AsyncSession = Depends(get_db)):
    case = (
        await db.execute(select(BenchmarkCase).where(BenchmarkCase.id == case_id))
    ).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="金标案例不存在")
    if case.verification_status != "VERIFIED":
        raise HTTPException(status_code=409, detail="该案例仍是待确认草稿，请人工核对标准答案后再运行回归")
    if not case.is_active:
        raise HTTPException(status_code=409, detail="该案例当前未启用，不能运行回归")
    if case.dataset_role == "HOLDOUT":
        raise HTTPException(status_code=409, detail="保密测试集不能单案例反复测试，只能参加完整发布门禁")
    result = await EvaluationService.run_benchmark_evaluation(db, benchmark_ids=[case_id])
    return {"code": 0, "message": "单案例回归完成", "data": result}

@router.post("/evaluation/run", summary="一键执行全量金标回归评测")
async def run_regression_evaluation(db: AsyncSession = Depends(get_db)):
    result = await _run_layered_evaluation(db)
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

    # The web console supplies its completed background evaluation so publishing
    # remains a short transaction. The fallback preserves compatibility for old API clients.
    eval_res = None
    if payload.evaluation_job_id:
        evaluation_job = (
            await db.execute(select(AdminJob).where(AdminJob.id == payload.evaluation_job_id))
        ).scalar_one_or_none()
        if (
            not evaluation_job
            or evaluation_job.job_type != "BENCHMARK_EVALUATION"
            or evaluation_job.status != "COMPLETED"
            or (evaluation_job.input_payload or {}).get("benchmark_ids")
        ):
            raise HTTPException(status_code=409, detail="发布必须绑定一次已完成的全量金标后台回归任务")
        current_cases = (
            await db.execute(
                select(BenchmarkCase).where(
                    BenchmarkCase.is_active.is_(True),
                    BenchmarkCase.verification_status == "VERIFIED",
                ).order_by(BenchmarkCase.id.asc())
            )
        ).scalars().all()
        current_snapshot = [f"{case.id}:{case.updated_at.isoformat()}" for case in current_cases]
        if (evaluation_job.input_payload or {}).get("benchmark_snapshot") != current_snapshot:
            raise HTTPException(status_code=409, detail="金标评测集在回归后发生了变化，请重新运行全量回归再发布")
        eval_res = evaluation_job.result or {}
    else:
        eval_res = await _run_layered_evaluation(db)
    if not eval_res.get("can_release", False):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "金标回归评测未通过，禁止发布版本",
                "total_cases": eval_res.get("total_cases", 0),
                "overall_accuracy_percent": eval_res.get("overall_accuracy_percent", 0),
                "critical_regressions_count": eval_res.get("critical_regressions_count", 0),
            },
        )
    benchmark_score = f"{eval_res.get('overall_accuracy_percent', 0.0)}%"

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
    try:
        await db.commit()
        await db.refresh(ver)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="版本号已被其他发布请求占用") from exc

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


# ==========================================
# 5. Prompt laboratory (draft -> evaluate -> activate/rollback)
# ==========================================

async def _ensure_prompt_seed(db: AsyncSession) -> PromptVersion:
    active = await PromptService.get_active(db)
    if active:
        return active
    seed = PromptVersion(
        version_tag="builtin-v1",
        content=default_skill_runner.extract_prompt_template,
        status="ACTIVE",
        source="BUILTIN",
        optimization_goal="Initial prompt imported from skill_v3/prompts/extract.md",
        activated_at=utc_now(),
    )
    db.add(seed)
    try:
        await db.commit()
        await db.refresh(seed)
        return seed
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(select(PromptVersion).where(PromptVersion.version_tag == "builtin-v1"))
        ).scalar_one()
        if existing.status != "ACTIVE":
            await PromptService.activate(db, existing)
            existing.activated_at = utc_now()
            await db.commit()
        return existing


def _prompt_payload(item: PromptVersion) -> dict:
    return {
        "id": item.id,
        "version_tag": item.version_tag,
        "content": item.content,
        "status": item.status,
        "source": item.source,
        "optimization_goal": item.optimization_goal,
        "evidence_feedback_ids": item.evidence_feedback_ids or [],
        "parent_id": item.parent_id,
        "evaluation_run_id": item.evaluation_run_id,
        "iteration_number": item.iteration_number or 1,
        "source_job_id": item.source_job_id,
        "source_evaluation_job_id": item.source_evaluation_job_id,
        "created_at": item.created_at.isoformat(),
        "activated_at": item.activated_at.isoformat() if item.activated_at else None,
    }


@router.get("/prompts", summary="提示词版本列表")
async def list_prompt_versions(db: AsyncSession = Depends(get_db)):
    await _ensure_prompt_seed(db)
    items = (
        await db.execute(select(PromptVersion).order_by(PromptVersion.created_at.desc()).limit(50))
    ).scalars().all()
    return {"code": 0, "data": [_prompt_payload(item) for item in items]}


@router.post("/prompts", summary="手工保存候选提示词")
async def create_prompt_candidate(payload: PromptCreateRequest, db: AsyncSession = Depends(get_db)):
    PromptService.validate_template(payload.content)
    parent = await _ensure_prompt_seed(db)
    item = PromptVersion(
        version_tag=f"prompt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        content=payload.content,
        status="DRAFT",
        source="MANUAL",
        optimization_goal=payload.optimization_goal,
        evidence_feedback_ids=[],
        parent_id=parent.id,
        iteration_number=(parent.iteration_number or 1) + 1,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return {"code": 0, "message": "候选提示词已保存，尚未影响生产识别", "data": _prompt_payload(item)}


@router.post("/prompts/optimize", summary="调用模型生成候选提示词")
async def optimize_prompt_with_model(payload: PromptOptimizeRequest, db: AsyncSession = Depends(get_db)):
    active = await _ensure_prompt_seed(db)
    feedback_query = select(TaskFeedback).where(TaskFeedback.status.in_(["ACCEPTED", "RESOLVED"]))
    if payload.feedback_ids:
        feedback_query = feedback_query.where(TaskFeedback.id.in_(payload.feedback_ids))
    feedbacks = (await db.execute(feedback_query.order_by(TaskFeedback.created_at.desc()).limit(20))).scalars().all()
    evidence = [_feedback_evidence(fb) for fb in feedbacks]
    if not feedbacks:
        raise HTTPException(status_code=400, detail="没有找到所选的已审核反馈，请刷新反馈列表后重试")

    # Let the model propose only concise incremental rules. The application
    # performs the merge, preserving the complete production schema and all
    # required placeholders deterministically.
    meta_prompt = f"""你是货代单证结构化抽取提示词专家。请根据已审核反馈生成增量纠错规则。
目标：{payload.optimization_goal}
审核通过的错误证据：{json.dumps(evidence, ensure_ascii=False)[:30000]}

硬性要求：
1. 只提出能够由上述反馈支持的规则，不得凭空扩展业务口径。
2. 规则必须说明“在什么证据下填什么字段”，并强调原文无证据时留空。
3. 不要复述或重写完整生产提示词。
4. 只返回 JSON：{{"summary":"本次优化摘要","rules":["规则1","规则2"]}}。
5. rules 为 1 至 12 条，每条应简洁、可执行且不超过 500 字。
"""
    try:
        response = await asyncio.wait_for(
            default_skill_runner.call_llm(
                meta_prompt,
                max_tokens=2200,
                max_retries=0,
            ),
            timeout=130,
        )
        parsed = default_skill_runner._parse_json_object_response(
            response,
            require_cargo_fields=False,
        )
        rules = parsed.get("rules", []) if isinstance(parsed, dict) else []
        if not isinstance(rules, list):
            raise ValueError("rules 必须是数组")
        rules = [str(rule).strip() for rule in rules if str(rule).strip()][:12]
        if not rules:
            raise ValueError("模型没有返回可用的增量规则")
        if any(len(rule) > 500 for rule in rules):
            raise ValueError("单条增量规则超过 500 字")
        summary = str(parsed.get("summary", "")).strip()[:500]
        rule_block = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1))
        optimized = (
            f"{active.content.rstrip()}\n\n"
            "### 反馈驱动候选规则（需回归验证）\n"
            f"优化目标：{payload.optimization_goal}\n"
            f"优化摘要：{summary or '根据所选已审核反馈生成增量规则'}\n"
            f"{rule_block}\n"
        )
        PromptService.validate_template(optimized)
    except asyncio.TimeoutError as exc:
        logger.warning("Prompt optimization timed out after 130 seconds")
        raise HTTPException(
            status_code=504,
            detail="模型生成超时（130 秒）。本次未创建候选版本，请稍后重试或检查模型服务状态",
        ) from exc
    except Exception as exc:
        logger.warning("Prompt optimization response rejected: %s", exc)
        raise HTTPException(status_code=502, detail=f"模型返回的候选提示词格式无效: {exc}") from exc
    item = PromptVersion(
        version_tag=f"V{(active.iteration_number or 1) + 1}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        content=optimized,
        status="DRAFT",
        source="AI",
        optimization_goal=payload.optimization_goal,
        evidence_feedback_ids=[fb.id for fb in feedbacks],
        parent_id=active.id,
        iteration_number=(active.iteration_number or 1) + 1,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return {"code": 0, "message": "模型已生成候选版本；必须回归通过后才能启用", "data": _prompt_payload(item)}


def _feedback_evidence(fb: TaskFeedback) -> dict:
    rows = build_field_diff_rows(fb.corrected_result or {}, fb.original_result or {})
    changed = [row for row in rows if not row.get("is_match")]
    return {
        "feedback_id": fb.id,
        "task_id": fb.task_id,
        "tenant_id": fb.tenant_id,
        "document_type": fb.document_type or "GENERAL",
        "error_category": fb.error_category or "UNSPECIFIED",
        "diff_fields": fb.diff_fields or [],
        "field_diffs": [
            {"field": row["field"], "actual": row["actual"], "expected": row["expected"]}
            for row in changed
        ],
        "original_result": fb.original_result or {},
        "human_corrected_result": fb.corrected_result or {},
        "source_context": {
            "mail_subject": getattr(fb.task, "mail_subject", None),
            "input_summary": getattr(fb.task, "input_summary", None),
        },
        "customer_notes": (fb.notes or "")[:4000],
        "review_guidance": (fb.review_comment or "")[:1000],
    }


async def _load_selected_feedbacks(db: AsyncSession, feedback_ids: List[str]) -> List[TaskFeedback]:
    if not feedback_ids:
        return []
    rows = (
        await db.execute(
            select(TaskFeedback).where(
                TaskFeedback.id.in_(feedback_ids),
                TaskFeedback.status.in_(["ACCEPTED", "RESOLVED"]),
            )
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    return [by_id[item] for item in feedback_ids if item in by_id]


@router.post("/prompts/evidence-preview", summary="预览发送给模型的反馈证据")
async def preview_prompt_evidence(payload: PromptOptimizeRequest, db: AsyncSession = Depends(get_db)):
    feedbacks = await _load_selected_feedbacks(db, payload.feedback_ids)
    if not feedbacks:
        raise HTTPException(status_code=400, detail="没有找到所选的已审核反馈")
    return {"code": 0, "data": {"goal": payload.optimization_goal, "evidence": [_feedback_evidence(fb) for fb in feedbacks]}}


async def _merge_job_result(job_id: str, values: dict) -> None:
    """Merge diagnostic metadata without discarding evidence already attached to a job."""
    async with AsyncSessionLocal() as db:
        item = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one_or_none()
        current = dict(item.result or {}) if item else {}
    current.update(values)
    await AdminJobService.update(job_id, result=current)


async def _collect_prompt_model_response(job_id: str, prompt: str, max_tokens: int) -> tuple[str, dict]:
    """Prefer streaming, then transparently fall back when a provider emits no content deltas."""
    chunks: List[str] = []
    last_saved = 0
    stream_error = None
    try:
        async for delta in default_skill_runner.stream_llm(
            prompt, max_tokens=max_tokens,
            timeout_seconds=settings.PROMPT_LLM_STREAM_TIMEOUT_SECONDS,
        ):
            await AdminJobService.check_cancelled(job_id)
            chunks.append(delta)
            current = "".join(chunks)
            if len(current) - last_saved >= 120:
                last_saved = len(current)
                await AdminJobService.update(
                    job_id, stream_text=current, progress_current=len(current),
                    progress_total=0, progress_percent=35,
                )
    except httpx.TimeoutException:
        stream_error = "STREAM_TIMEOUT"
    response = "".join(chunks).strip()
    diagnostics = {
        "generation_mode": "STREAM",
        "stream_chunk_count": len(chunks),
        "stream_content_chars": len(response),
        "fallback_used": False,
        "stream_error": stream_error,
    }
    if response and not stream_error:
        return response, diagnostics

    diagnostics.update({"generation_mode": "NON_STREAM_FALLBACK", "fallback_used": True})
    await _merge_job_result(job_id, {"generation_diagnostics": diagnostics})
    await AdminJobService.update(
        job_id, phase="COMPATIBILITY_RETRY", progress_current=0,
        progress_total=1, progress_percent=55,
    )
    await AdminJobService.check_cancelled(job_id)
    try:
        response = (await default_skill_runner.call_llm(
            prompt, max_tokens=max_tokens, max_retries=0,
            timeout_seconds=settings.PROMPT_LLM_FALLBACK_TIMEOUT_SECONDS,
        )).strip()
    except RuntimeError as exc:
        if "Timeout" in str(exc):
            raise httpx.ReadTimeout(
                f"兼容模式等待模型超过 {settings.PROMPT_LLM_FALLBACK_TIMEOUT_SECONDS} 秒"
            ) from exc
        raise
    diagnostics["fallback_content_chars"] = len(response)
    await _merge_job_result(job_id, {"generation_diagnostics": diagnostics})
    if not response:
        raise ValueError("模型接口已响应，但流式模式和兼容模式都没有返回正文")
    await AdminJobService.update(
        job_id, stream_text=response, progress_current=len(response),
        progress_total=len(response), progress_percent=80,
    )
    return response, diagnostics


async def _run_prompt_generation_job(job_id: str) -> None:
    await AdminJobService.update(
        job_id, status="RUNNING", phase="LOADING_EVIDENCE", started_at=utc_now(), progress_percent=5
    )
    async with AsyncSessionLocal() as db:
        job = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one()
        payload = job.input_payload or {}
        feedbacks = await _load_selected_feedbacks(db, payload.get("feedback_ids") or [])
        active = await _ensure_prompt_seed(db)
    if not feedbacks:
        raise ValueError("没有找到所选的已审核反馈")
    evidence = [_feedback_evidence(fb) for fb in feedbacks]
    evidence_json = json.dumps(evidence, ensure_ascii=False)
    existing_prompt = active.content
    if len(evidence_json) + len(existing_prompt) > 220_000:
        raise ValueError("所选完整反馈证据超过模型上下文安全上限，请减少本次反馈数量后重试")
    await AdminJobService.update(
        job_id,
        phase="MODEL_STREAMING",
        progress_total=len(feedbacks),
        progress_current=0,
        progress_percent=15,
        result={"evidence": evidence},
    )
    meta_prompt = f"""你是货代单证结构化抽取提示词专家。根据人工审核反馈生成可执行增量规则。
优化目标：{payload.get('optimization_goal', '')}
人工证据（与预览完全一致，未截断）：{evidence_json}
当前完整生产提示词（用于识别冲突）：{existing_prompt}

要求：
1. 每条规则必须能够追溯到证据，source_feedback_ids 只能填写上述 feedback_id。
2. actual 是系统原值，expected 是人工确认值；原文无证据必须留空，禁止猜测。
3. action 只能是 ADD、MODIFY、DELETE；MODIFY/DELETE 必须提供完全匹配的 target_rule。
4. 如与已有规则含义相反，填写 conflict_reason；否则为空字符串。
5. affected_fields 填受影响字段。
6. 只返回 JSON：{{"summary":"摘要","rules":[{{"text":"规则","source_feedback_ids":["fb_x"],"affected_fields":["ContainerInfo"],"action":"ADD","target_rule":"","conflict_reason":""}}]}}。
"""
    response, generation_diagnostics = await _collect_prompt_model_response(
        job_id, meta_prompt, max_tokens=2200
    )
    await AdminJobService.update(job_id, stream_text=response, phase="VALIDATING", progress_percent=90)
    parsed = default_skill_runner._parse_json_object_response(response, require_cargo_fields=False)
    raw_rules = parsed.get("rules") if isinstance(parsed, dict) else None
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("模型响应格式错误：没有返回 rules 数组")
    allowed_ids = {fb.id for fb in feedbacks}
    rules = []
    for index, raw in enumerate(raw_rules[:20], 1):
        if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
            continue
        sources = [item for item in raw.get("source_feedback_ids", []) if item in allowed_ids]
        if not sources:
            raise ValueError(f"规则 {index} 缺少有效的来源反馈，已拒绝无证据规则")
        rules.append({
            "id": f"rule_{index}",
            "text": str(raw["text"]).strip()[:1000],
            "selected": True,
            "source_feedback_ids": sources,
            "affected_fields": [str(x)[:128] for x in raw.get("affected_fields", [])[:30]],
            "action": raw.get("action") if raw.get("action") in {"ADD", "MODIFY", "DELETE"} else "ADD",
            "target_rule": str(raw.get("target_rule", "")).strip()[:1000],
            "conflict_reason": str(raw.get("conflict_reason", "")).strip()[:1000],
        })
    if not rules:
        raise ValueError("模型响应格式错误：没有可用规则")
    await AdminJobService.update(
        job_id,
        status="COMPLETED",
        phase="AWAITING_REVIEW",
        progress_percent=100,
        result={
            "summary": str(parsed.get("summary", ""))[:1000],
            "rules": rules,
            "evidence": evidence,
            "active_prompt_id": active.id,
            "generation_diagnostics": generation_diagnostics,
        },
        finished_at=utc_now(),
    )


@router.post("/prompts/optimization-jobs", summary="创建流式提示词优化任务")
async def create_prompt_optimization_job(payload: PromptOptimizeRequest, db: AsyncSession = Depends(get_db)):
    feedbacks = await _load_selected_feedbacks(db, payload.feedback_ids)
    if not feedbacks:
        raise HTTPException(status_code=400, detail="没有找到所选的已审核反馈")
    item = AdminJob(
        job_type="PROMPT_GENERATION",
        input_payload=payload.model_dump(),
        progress_total=len(feedbacks),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    AdminJobService.schedule(item.id, _run_prompt_generation_job)
    return {"code": 0, "message": "提示词优化任务已在后台启动", "data": job_payload(item)}


PROMPT_REFINEMENT_DIRECTION_LABELS = {
    "FIX_REMAINING_NON_CRITICAL": "修复剩余非关键字段",
    "USE_NEW_FEEDBACK": "使用新的人工反馈",
    "SIMPLIFY_MERGE_RULES": "简化和合并重复规则",
    "REDUCE_GUESSING": "降低猜测与幻觉",
    "CUSTOM": "自定义目标",
}


async def _run_prompt_refinement_job(job_id: str) -> None:
    await AdminJobService.update(
        job_id, status="RUNNING", phase="LOADING_FAILED_CASES", started_at=utc_now(), progress_percent=5
    )
    async with AsyncSessionLocal() as db:
        job = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one()
        payload = job.input_payload or {}
        evaluation_job = (
            await db.execute(select(AdminJob).where(AdminJob.id == payload.get("evaluation_job_id")))
        ).scalar_one_or_none()
        if not evaluation_job or evaluation_job.job_type != "PROMPT_EVALUATION" or evaluation_job.status != "COMPLETED":
            raise ValueError("来源 A/B 评测任务不存在或尚未完成")
        evaluation_result = evaluation_job.result or {}
        prompt_id = (evaluation_job.input_payload or {}).get("prompt_id")
        parent = (
            await db.execute(select(PromptVersion).where(PromptVersion.id == prompt_id))
        ).scalar_one_or_none()
        if not parent:
            raise ValueError("来源候选提示词不存在")
        selected_feedbacks = await _load_selected_feedbacks(db, payload.get("feedback_ids") or [])

    comparison = evaluation_result.get("ab_comparison") or build_ab_comparison(
        evaluation_result.get("baseline") or {}, evaluation_result.get("candidate") or {}
    )
    failed_cases = []
    allowed_case_ids = set()
    for case in comparison.get("cases", []):
        if case.get("dataset_role", "TRAIN") != "TRAIN":
            continue
        failed_fields = [
            field for field in case.get("field_comparisons", [])
            if field.get("classification") in {"REGRESSED", "STILL_WRONG"}
        ]
        if failed_fields:
            allowed_case_ids.add(case.get("case_id"))
            failed_cases.append({
                "case_id": case.get("case_id"),
                "title": case.get("title"),
                "doc_type": case.get("doc_type"),
                "source_files": case.get("source_files") or [],
                "input_text": (case.get("input_text") or "")[:12000],
                "field_failures": failed_fields,
            })
    reference_cases = []
    if not failed_cases:
        for case in comparison.get("cases", [])[:20]:
            if case.get("dataset_role", "TRAIN") != "TRAIN":
                continue
            case_id = case.get("case_id")
            if not case_id:
                continue
            allowed_case_ids.add(case_id)
            reference_cases.append({
                "case_id": case_id,
                "title": case.get("title"),
                "doc_type": case.get("doc_type"),
                "candidate_accuracy_percent": case.get("candidate_accuracy_percent"),
                "field_comparisons": [
                    field for field in case.get("field_comparisons", [])
                    if field.get("classification") != "UNCHANGED_CORRECT"
                ],
            })
    directions = payload.get("optimization_directions") or []
    direction_labels = [PROMPT_REFINEMENT_DIRECTION_LABELS[item] for item in directions if item in PROMPT_REFINEMENT_DIRECTION_LABELS]
    instruction = (payload.get("optimization_instruction") or payload.get("optimization_goal") or "").strip()
    feedback_evidence = [_feedback_evidence(item) for item in selected_feedbacks]
    evidence = {
        "evaluation_job_id": evaluation_job.id,
        "refinement_mode": "FAILED_RECOVERY" if failed_cases else "CONTINUOUS_IMPROVEMENT",
        "optimization_directions": direction_labels,
        "human_instruction": instruction,
        "gate_reasons": comparison.get("gate_reasons") or [],
        "summary": comparison.get("summary") or {},
        "failed_cases": failed_cases,
        "reference_cases": reference_cases,
        "reviewed_feedback": feedback_evidence,
    }
    evidence_json = json.dumps(evidence, ensure_ascii=False)
    if len(evidence_json) + len(parent.content) > 220_000:
        raise ValueError("失败案例证据超过模型上下文安全上限，请缩小金标集后重试")
    await AdminJobService.update(
        job_id, phase="MODEL_STREAMING", progress_percent=15,
        result={"evidence": evidence, "parent_prompt_id": parent.id, "source_evaluation_job_id": evaluation_job.id},
    )
    meta_prompt = f"""你是货代单证抽取系统的提示词诊断专家。请基于本次 A/B 金标评测和人工指定目标继续优化候选提示词；即使门禁已经通过，也要在不破坏现有正确结果的前提下完成所选方向。
优化方向（可多选）：{'；'.join(direction_labels) or '基于评测结果持续提升'}
人工补充指示：{instruction or '无额外指示'}
评测与人工证据：{evidence_json}
当前候选提示词：{parent.content}

要求：
1. 先判断每个错误属于 PROMPT、NORMALIZATION、EVALUATOR、GOLD、OCR 或 UNKNOWN；数值格式等价等评测器问题不要错误地产生抽取规则。
2. 每条规则必须引用 source_case_ids 或 source_feedback_ids；ID 只能来自上述评测案例或已审核反馈。
3. 规则必须是结构化的 ADD/MODIFY/DELETE；MODIFY/DELETE 必须给出当前提示词中完整匹配的 target_rule。
4. 指出 expected_effect、risk_fields、confidence；若与现有规则冲突则填写 conflict_reason。
5. 只返回 JSON：{{"summary":"摘要","diagnoses":[{{"field":"字段","classification":"REGRESSED或STILL_WRONG","error_type":"PROMPT","reason":"原因","source_case_ids":["case_id"]}}],"rules":[{{"text":"规则","source_case_ids":["case_id"],"source_feedback_ids":["feedback_id"],"affected_fields":["字段"],"action":"ADD","target_rule":"","conflict_reason":"","diagnosis":"为什么这样改","error_type":"PROMPT","expected_effect":"预期效果","risk_fields":[],"confidence":0.8}}]}}。
"""
    response, generation_diagnostics = await _collect_prompt_model_response(
        job_id, meta_prompt, max_tokens=3200
    )
    await AdminJobService.update(job_id, stream_text=response, phase="VALIDATING", progress_percent=90)
    parsed = default_skill_runner._parse_json_object_response(response, require_cargo_fields=False)
    raw_rules = parsed.get("rules") if isinstance(parsed, dict) else None
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("模型响应格式错误：没有返回 rules 数组")
    allowed_error_types = {"PROMPT", "NORMALIZATION", "EVALUATOR", "GOLD", "OCR", "UNKNOWN"}
    allowed_feedback_ids = {item.id for item in selected_feedbacks}
    rules = []
    for index, raw in enumerate(raw_rules[:20], 1):
        if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
            continue
        sources = [item for item in raw.get("source_case_ids", []) if item in allowed_case_ids]
        feedback_sources = [item for item in raw.get("source_feedback_ids", []) if item in allowed_feedback_ids]
        if not sources and not feedback_sources:
            raise ValueError(f"规则 {index} 缺少有效的来源案例或人工反馈")
        error_type = raw.get("error_type") if raw.get("error_type") in allowed_error_types else "UNKNOWN"
        rules.append({
            "id": f"rule_{index}", "text": str(raw["text"]).strip()[:1000], "selected": True,
            "source_feedback_ids": feedback_sources, "source_case_ids": sources,
            "affected_fields": [str(x)[:128] for x in raw.get("affected_fields", [])[:30]],
            "action": raw.get("action") if raw.get("action") in {"ADD", "MODIFY", "DELETE"} else "ADD",
            "target_rule": str(raw.get("target_rule", "")).strip()[:1000],
            "conflict_reason": str(raw.get("conflict_reason", "")).strip()[:1000],
            "diagnosis": str(raw.get("diagnosis", "")).strip()[:2000], "error_type": error_type,
            "expected_effect": str(raw.get("expected_effect", "")).strip()[:1000],
            "risk_fields": [str(x)[:128] for x in raw.get("risk_fields", [])[:30]],
            "confidence": max(0, min(1, float(raw.get("confidence", 0)))) if raw.get("confidence") is not None else None,
        })
    if not rules:
        raise ValueError("模型响应格式错误：没有可审核的规则")
    raw_diagnoses = parsed.get("diagnoses") if isinstance(parsed.get("diagnoses"), list) else []
    diagnoses = []
    for raw in raw_diagnoses[:100]:
        if not isinstance(raw, dict):
            continue
        sources = [item for item in raw.get("source_case_ids", []) if item in allowed_case_ids]
        if not sources:
            continue
        diagnoses.append({
            "field": str(raw.get("field", ""))[:256],
            "classification": raw.get("classification") if raw.get("classification") in {"REGRESSED", "STILL_WRONG"} else "STILL_WRONG",
            "error_type": raw.get("error_type") if raw.get("error_type") in allowed_error_types else "UNKNOWN",
            "reason": str(raw.get("reason", ""))[:2000],
            "source_case_ids": sources,
        })
    await AdminJobService.update(
        job_id, status="COMPLETED", phase="AWAITING_REVIEW", progress_percent=100,
        result={
            "summary": str(parsed.get("summary", ""))[:1000], "diagnoses": diagnoses,
            "rules": rules, "evidence": evidence, "parent_prompt_id": parent.id,
            "source_evaluation_job_id": evaluation_job.id,
            "iteration_number": (parent.iteration_number or 1) + 1,
            "generation_diagnostics": generation_diagnostics,
        }, finished_at=utc_now(),
    )


@router.post("/prompts/refinement-jobs", summary="基于失败 A/B 评测继续优化")
async def create_prompt_refinement_job(payload: PromptRefineRequest, db: AsyncSession = Depends(get_db)):
    source = (await db.execute(select(AdminJob).where(AdminJob.id == payload.evaluation_job_id))).scalar_one_or_none()
    if not source or source.job_type != "PROMPT_EVALUATION" or source.status != "COMPLETED":
        raise HTTPException(status_code=404, detail="已完成的 A/B 评测任务不存在")
    if (source.result or {}).get("holdout_only_failure"):
        raise HTTPException(
            status_code=409,
            detail="本次只有保密测试集未通过。为防止泄题，不能把失败内容直接交给 AI；请补充新的优化集反馈后再优化。",
        )
    if not payload.optimization_directions and not (payload.optimization_instruction or payload.optimization_goal or "").strip():
        raise HTTPException(status_code=400, detail="请至少选择一个优化方向，或填写人工补充指示")
    if "USE_NEW_FEEDBACK" in payload.optimization_directions and not payload.feedback_ids:
        raise HTTPException(status_code=400, detail="选择“使用新的人工反馈”时，请先勾选至少一条已审核反馈")
    if payload.feedback_ids:
        selected_feedbacks = await _load_selected_feedbacks(db, payload.feedback_ids)
        selected_ids = {item.id for item in selected_feedbacks}
        missing_ids = [item for item in dict.fromkeys(payload.feedback_ids) if item not in selected_ids]
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"以下反馈不存在或尚未审核通过：{', '.join(missing_ids[:10])}",
            )
    item = AdminJob(job_type="PROMPT_REFINEMENT", input_payload=payload.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    AdminJobService.schedule(item.id, _run_prompt_refinement_job)
    return {"code": 0, "message": "已按所选方向启动下一版流式优化", "data": job_payload(item)}


@router.post("/prompts/optimization-jobs/{job_id}/finalize", summary="审核规则并保存候选提示词")
@router.post("/prompts/refinement-jobs/{job_id}/finalize", summary="审核失败诊断规则并保存下一版")
async def finalize_prompt_job(job_id: str, payload: PromptFinalizeRequest, db: AsyncSession = Depends(get_db)):
    job = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one_or_none()
    if not job or job.job_type not in {"PROMPT_GENERATION", "PROMPT_REFINEMENT"}:
        raise HTTPException(status_code=404, detail="提示词优化任务不存在")
    if job.status != "COMPLETED":
        raise HTTPException(status_code=409, detail="任务尚未生成完成")
    if job.related_entity_id:
        raise HTTPException(status_code=409, detail="该生成任务已经定稿，不能重复创建候选版本")
    generated = job.result or {}
    active = await _ensure_prompt_seed(db)
    if job.job_type == "PROMPT_GENERATION":
        parent = active
        if generated.get("active_prompt_id") != active.id:
            raise HTTPException(status_code=409, detail="生成后生产提示词已发生变化，请重新生成以避免覆盖新规则")
    else:
        parent = (await db.execute(select(PromptVersion).where(PromptVersion.id == generated.get("parent_prompt_id")))).scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=409, detail="来源候选提示词已不存在")
    allowed_feedback_ids = set((job.input_payload or {}).get("feedback_ids") or [])
    refinement_evidence = generated.get("evidence") if isinstance(generated.get("evidence"), dict) else {}
    allowed_case_ids = {
        item.get("case_id") for item in [
            *(refinement_evidence.get("failed_cases") or []),
            *(refinement_evidence.get("reference_cases") or []),
        ]
        if isinstance(item, dict)
    }
    content = parent.content.rstrip()
    added, modified, deleted = [], [], []
    for rule in payload.rules:
        if not rule.selected:
            continue
        if job.job_type == "PROMPT_GENERATION":
            if not rule.source_feedback_ids or not set(rule.source_feedback_ids).issubset(allowed_feedback_ids):
                raise HTTPException(status_code=409, detail="规则来源反馈缺失或不属于本次证据集")
        elif not (
            (rule.source_case_ids and set(rule.source_case_ids).issubset(allowed_case_ids))
            or (rule.source_feedback_ids and set(rule.source_feedback_ids).issubset(allowed_feedback_ids))
        ):
            raise HTTPException(status_code=409, detail="规则来源案例或人工反馈缺失，或不属于本次证据集")
        text_value = rule.text.strip()
        if rule.action == "ADD":
            added.append(text_value)
        elif rule.action == "MODIFY":
            target = (rule.target_rule or "").strip()
            if not target or target not in content:
                raise HTTPException(status_code=409, detail=f"待修改规则不存在: {target[:80]}")
            content = content.replace(target, text_value, 1)
            modified.append({"from": target, "to": text_value})
        else:
            target = (rule.target_rule or "").strip()
            if not target or target not in content:
                raise HTTPException(status_code=409, detail=f"待删除规则不存在: {target[:80]}")
            content = content.replace(target, "", 1)
            deleted.append(target)
    if added:
        block = "\n".join(f"{i}. {rule}" for i, rule in enumerate(added, 1))
        content += f"\n\n### 反馈驱动候选规则（需回归验证）\n{block}\n"
    if not added and not modified and not deleted:
        raise HTTPException(status_code=400, detail="没有选择任何有效规则，未创建候选版本")
    PromptService.validate_template(content)
    input_payload = job.input_payload or {}
    item = PromptVersion(
        version_tag=f"V{(parent.iteration_number or 1) + 1}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        content=content,
        status="DRAFT",
        source="AI",
        optimization_goal=(input_payload.get("optimization_instruction") or input_payload.get("optimization_goal") or "；".join(
            PROMPT_REFINEMENT_DIRECTION_LABELS[item]
            for item in (input_payload.get("optimization_directions") or [])
            if item in PROMPT_REFINEMENT_DIRECTION_LABELS
        )),
        evidence_feedback_ids=list(dict.fromkeys([
            *(parent.evidence_feedback_ids or []), *(input_payload.get("feedback_ids") or [])
        ])),
        parent_id=parent.id,
        iteration_number=(parent.iteration_number or 1) + 1,
        source_job_id=job.id,
        source_evaluation_job_id=generated.get("source_evaluation_job_id"),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    result = dict(job.result or {})
    result.update({"prompt_version_id": item.id, "change_set": {"added": added, "modified": modified, "deleted": deleted}})
    job.result = result
    job.related_entity_id = item.id
    await db.commit()
    return {"code": 0, "message": "已按人工确认的规则保存候选提示词", "data": {"prompt": _prompt_payload(item), "change_set": result["change_set"]}}


async def _job_progress(job_id: str, current: int, total: int, case_result: dict) -> None:
    await AdminJobService.update(
        job_id,
        phase="EVALUATING",
        progress_current=current,
        progress_total=total,
        progress_percent=round(current * 100 / total) if total else 0,
        result={"latest_case": {
            "case_id": case_result.get("case_id"),
            "title": case_result.get("title"),
            "is_passed": case_result.get("is_passed"),
            "accuracy_percent": case_result.get("accuracy_percent"),
        }},
    )


async def _job_stage_progress(job_id: str, stage: str, details: dict) -> None:
    """Persist real extraction stages so a long first case does not look frozen."""
    stage_percent = {
        "READING_GOLD_FILE": 5,
        "PDF_TO_IMAGE": 15,
        "VISION_OCR": 25,
        "PREPROCESS_CACHE_HIT": 30,
        "MAIN_MODEL_REQUEST": 45,
        "PARSING_MODEL_JSON": 72,
        "FIELD_COMPARISON": 84,
        "GENERATING_REPORT": 94,
    }
    attempt = int(details.get("attempt") or 0)
    total_attempts = int(details.get("total_attempts") or 0)
    percent = stage_percent.get(stage, 0)
    if stage == "MAIN_MODEL_REQUEST" and total_attempts:
        percent = min(68, 36 + round(attempt * 30 / total_attempts))
    await AdminJobService.update(
        job_id,
        phase=stage,
        progress_current=attempt if stage == "MAIN_MODEL_REQUEST" else percent,
        progress_total=total_attempts if stage == "MAIN_MODEL_REQUEST" else 100,
        progress_percent=percent,
        result={"stage_detail": details},
    )


async def _run_regression_job(job_id: str) -> None:
    await AdminJobService.update(job_id, status="RUNNING", phase="PREPARING", started_at=utc_now())
    async with AsyncSessionLocal() as db:
        job = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one()
        payload = job.input_payload or {}
        job_type = job.job_type

        async def progress(current, total, result):
            await _job_progress(job_id, current, total, result)

        async def stage_progress(stage, details):
            await _job_stage_progress(job_id, stage, details)

        async def cancel_check():
            await AdminJobService.check_cancelled(job_id)

        if job_type == "BENCHMARK_EVALUATION":
            if payload.get("benchmark_ids"):
                result = await EvaluationService.run_benchmark_evaluation(
                    db,
                    benchmark_ids=payload.get("benchmark_ids"),
                    progress_callback=progress,
                    cancel_check=cancel_check,
                    stage_callback=stage_progress,
                )
            else:
                result = await _run_layered_evaluation(
                    db, progress_callback=progress, cancel_check=cancel_check,
                    stage_callback=stage_progress,
                )
        elif job_type == "FEW_SHOT_EVALUATION":
            fs = (await db.execute(select(FewShotExample).where(FewShotExample.id == payload.get("few_shot_id")))).scalar_one_or_none()
            if not fs:
                raise ValueError("Few-Shot 候选不存在")
            snippet = (
                "\n### 待验证的纠错参考案例（仅本次回归使用）\n"
                f"输入片段：\n{fs.input_excerpt}\n"
                f"期望输出：\n{json.dumps(fs.expected_output, ensure_ascii=False, indent=2)}\n"
            )
            result = await _run_layered_evaluation(
                db,
                extra_few_shot_snippet=snippet,
                exclude_feedback_id=fs.feedback_id,
                progress_callback=progress,
                cancel_check=cancel_check,
                stage_callback=stage_progress,
            )
            fs.evaluation_run_id = result.get("evaluation_run_id")
            fs.lifecycle_status = "VALIDATED" if result.get("can_release") else "FAILED"
            fs.is_active = False
            await db.commit()
        elif job_type == "PROMPT_EVALUATION":
            item = (await db.execute(select(PromptVersion).where(PromptVersion.id == payload.get("prompt_id")))).scalar_one_or_none()
            if not item:
                raise ValueError("提示词候选不存在")
            active = await _ensure_prompt_seed(db)
            prepared_payload_cache = {}
            await AdminJobService.update(job_id, phase="BASELINE_EVALUATION")
            baseline_train = await EvaluationService.run_benchmark_evaluation(
                db,
                prompt_template=active.content,
                prompt_version_id=active.id,
                dataset_role="TRAIN",
                progress_callback=progress,
                cancel_check=cancel_check,
                stage_callback=stage_progress,
                prepared_payload_cache=prepared_payload_cache,
                evaluation_label="生产版本 · 优化集",
            )
            baseline_holdout = await EvaluationService.run_benchmark_evaluation(
                db, prompt_template=active.content, prompt_version_id=active.id,
                dataset_role="HOLDOUT", progress_callback=progress, cancel_check=cancel_check,
                stage_callback=stage_progress,
                prepared_payload_cache=prepared_payload_cache,
                evaluation_label="生产版本 · 保密集",
            )
            await AdminJobService.update(job_id, phase="CANDIDATE_EVALUATION", progress_current=0, progress_percent=0)
            candidate_train = await EvaluationService.run_benchmark_evaluation(
                db,
                prompt_template=item.content,
                prompt_version_id=item.id,
                dataset_role="TRAIN",
                progress_callback=progress,
                cancel_check=cancel_check,
                stage_callback=stage_progress,
                prepared_payload_cache=prepared_payload_cache,
                evaluation_label="候选版本 · 优化集",
            )
            candidate_holdout = await EvaluationService.run_benchmark_evaluation(
                db, prompt_template=item.content, prompt_version_id=item.id,
                dataset_role="HOLDOUT", progress_callback=progress, cancel_check=cancel_check,
                stage_callback=stage_progress,
                prepared_payload_cache=prepared_payload_cache,
                evaluation_label="候选版本 · 保密集",
            )
            baseline = _layered_evaluation_result(baseline_train, baseline_holdout)
            candidate = _layered_evaluation_result(candidate_train, candidate_holdout)
            comparison = build_ab_comparison(baseline_train, candidate_train)
            holdout_comparison = build_ab_comparison(baseline_holdout, candidate_holdout)
            no_regression = bool(
                baseline_train.get("total_cases") and baseline_holdout.get("total_cases")
                and comparison["can_release"] and holdout_comparison["can_release"]
            )
            gate_checks = [
                *[{**check, "label": f"优化集：{check['label']}"} for check in comparison["gate_checks"]],
                *[{**check, "label": f"保密集：{check['label']}"} for check in holdout_comparison["gate_checks"]],
            ]
            if not baseline_holdout.get("total_cases"):
                gate_checks.append({"code": "HOLDOUT_PRESENT", "label": "保密集：至少配置一个案例", "passed": False, "detail": "请在金标评测集设置保密测试案例"})
            item.evaluation_run_id = candidate_holdout.get("evaluation_run_id") or candidate_train.get("evaluation_run_id")
            item.status = "VALIDATED" if no_regression else "FAILED"
            await db.commit()
            result = {
                **candidate,
                "baseline": baseline,
                "candidate": candidate,
                "accuracy_delta": round(candidate.get("overall_accuracy_percent", 0) - baseline.get("overall_accuracy_percent", 0), 1),
                "no_regression": bool(no_regression),
                "ab_comparison": comparison,
                "holdout_comparison": {
                    "summary": holdout_comparison["summary"],
                    "gate_checks": holdout_comparison["gate_checks"],
                    "gate_reasons": holdout_comparison["gate_reasons"],
                    "can_release": holdout_comparison["can_release"],
                    "baseline_accuracy_percent": baseline_holdout.get("overall_accuracy_percent", 0),
                    "candidate_accuracy_percent": candidate_holdout.get("overall_accuracy_percent", 0),
                    "total_cases": candidate_holdout.get("total_cases", 0),
                },
                "holdout_only_failure": bool(comparison["can_release"] and not holdout_comparison["can_release"]),
                "gate_checks": gate_checks,
                "gate_reasons": [check["detail"] for check in gate_checks if not check["passed"]],
            }
        elif job_type == "PROMPT_QUICK_EVALUATION":
            item = (await db.execute(select(PromptVersion).where(PromptVersion.id == payload.get("prompt_id")))).scalar_one_or_none()
            if not item:
                raise ValueError("提示词候选不存在")
            benchmark_ids = payload.get("benchmark_ids") or []
            if not benchmark_ids:
                raise ValueError("没有可快速回归的历史失败案例")
            result = await EvaluationService.run_benchmark_evaluation(
                db, prompt_template=item.content, prompt_version_id=item.id,
                benchmark_ids=benchmark_ids, progress_callback=progress, cancel_check=cancel_check,
                stage_callback=stage_progress,
            )
            result.update({
                "quick_regression": True,
                "source_evaluation_job_id": payload.get("source_evaluation_job_id"),
                "failed_case_ids": benchmark_ids,
                "all_failed_cases_fixed": result.get("failed_cases", 0) == 0,
                "notice": "快速回归只验证历史失败案例，不替代发布前的完整 A/B 回归。",
            })
        else:
            raise ValueError(f"不支持的任务类型: {job_type}")
    await AdminJobService.update(
        job_id,
        status="COMPLETED",
        phase="COMPLETED",
        progress_percent=100,
        result=result,
        finished_at=utc_now(),
    )


async def _create_regression_job(db: AsyncSession, job_type: str, payload: dict, related_id: Optional[str] = None):
    item = AdminJob(job_type=job_type, input_payload=payload, related_entity_id=related_id)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    AdminJobService.schedule(item.id, _run_regression_job)
    return {"code": 0, "message": "后台任务已启动，可离开页面后稍后查看", "data": job_payload(item)}


@router.post("/jobs/benchmark-evaluation", summary="后台运行全量金标回归")
async def create_benchmark_evaluation_job(db: AsyncSession = Depends(get_db)):
    cases = (
        await db.execute(
            select(BenchmarkCase).where(
                BenchmarkCase.is_active.is_(True),
                BenchmarkCase.verification_status == "VERIFIED",
            ).order_by(BenchmarkCase.id.asc())
        )
    ).scalars().all()
    snapshot = [f"{case.id}:{case.updated_at.isoformat()}" for case in cases]
    return await _create_regression_job(
        db, "BENCHMARK_EVALUATION", {"benchmark_snapshot": snapshot}
    )


@router.post("/jobs/benchmark-evaluation/{case_id}", summary="后台运行单案例金标回归")
async def create_single_benchmark_evaluation_job(case_id: str, db: AsyncSession = Depends(get_db)):
    case = (await db.execute(select(BenchmarkCase).where(BenchmarkCase.id == case_id))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="金标案例不存在")
    if case.verification_status != "VERIFIED" or not case.is_active:
        raise HTTPException(status_code=409, detail="案例必须已人工确认且启用才能运行回归")
    return await _create_regression_job(
        db, "BENCHMARK_EVALUATION", {"benchmark_ids": [case_id]}, case_id
    )


@router.post("/jobs/few-shot-evaluation/{fs_id}", summary="后台验证 Few-Shot")
async def create_few_shot_evaluation_job(fs_id: str, db: AsyncSession = Depends(get_db)):
    return await _create_regression_job(db, "FEW_SHOT_EVALUATION", {"few_shot_id": fs_id}, fs_id)


@router.post("/jobs/prompt-evaluation/{prompt_id}", summary="后台运行提示词 A/B 回归")
async def create_prompt_evaluation_job(prompt_id: str, db: AsyncSession = Depends(get_db)):
    return await _create_regression_job(db, "PROMPT_EVALUATION", {"prompt_id": prompt_id}, prompt_id)


@router.post("/jobs/prompt-quick-evaluation/{prompt_id}", summary="快速回归上一轮失败案例")
async def create_prompt_quick_evaluation_job(prompt_id: str, db: AsyncSession = Depends(get_db)):
    item = (await db.execute(select(PromptVersion).where(PromptVersion.id == prompt_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="提示词候选不存在")
    if not item.source_evaluation_job_id:
        raise HTTPException(status_code=409, detail="该版本没有来源失败评测，请先运行完整 A/B 回归")
    source = (await db.execute(select(AdminJob).where(AdminJob.id == item.source_evaluation_job_id))).scalar_one_or_none()
    comparison = ((source.result or {}).get("ab_comparison") or {}) if source else {}
    failed_ids = [
        case.get("case_id") for case in comparison.get("cases", [])
        if case.get("dataset_role", "TRAIN") == "TRAIN"
        and any(field.get("classification") in {"REGRESSED", "STILL_WRONG"} for field in case.get("field_comparisons", []))
    ]
    failed_ids = [case_id for case_id in dict.fromkeys(failed_ids) if case_id]
    if not failed_ids:
        raise HTTPException(status_code=409, detail="来源评测没有可快速回归的失败案例")
    return await _create_regression_job(
        db, "PROMPT_QUICK_EVALUATION",
        {"prompt_id": item.id, "benchmark_ids": failed_ids, "source_evaluation_job_id": source.id}, item.id,
    )


@router.get("/jobs", summary="查询后台任务")
async def list_admin_jobs(
    status_filter: Optional[str] = Query(None, alias="status"),
    job_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AdminJob)
    if status_filter:
        stmt = stmt.where(AdminJob.status == status_filter)
    if job_type:
        stmt = stmt.where(AdminJob.job_type == job_type)
    items = (await db.execute(stmt.order_by(AdminJob.created_at.desc()).limit(100))).scalars().all()
    return {"code": 0, "data": [job_payload(item) for item in items]}


@router.get("/jobs/{job_id}", summary="查询后台任务状态")
async def get_admin_job(job_id: str, db: AsyncSession = Depends(get_db)):
    item = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return {"code": 0, "data": job_payload(item)}


@router.post("/jobs/{job_id}/cancel", summary="取消后台任务")
async def cancel_admin_job(job_id: str, db: AsyncSession = Depends(get_db)):
    item = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    if item.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        return {"code": 0, "message": "任务已经结束", "data": job_payload(item)}
    await AdminJobService.cancel(job_id)
    return {"code": 0, "message": "已请求取消任务"}


@router.get("/jobs/{job_id}/stream", summary="流式读取任务进度")
async def stream_admin_job(job_id: str):
    async def events():
        last_signature = None
        while True:
            async with AsyncSessionLocal() as db:
                item = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one_or_none()
                if not item:
                    yield json.dumps({"event": "error", "message": "任务不存在"}, ensure_ascii=False) + "\n"
                    return
                data = job_payload(item)
            signature = (data["status"], data["phase"], data["progress_percent"], len(data["stream_text"]), data["error_code"])
            if signature != last_signature:
                yield json.dumps({"event": "update", "data": data}, ensure_ascii=False) + "\n"
                last_signature = signature
            if data["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                return
            await asyncio.sleep(0.5)
    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@router.get("/prompts/feedback-options", summary="提示词优化可选反馈")
async def list_prompt_feedback_options(
    field: Optional[str] = None,
    tenant_id: Optional[str] = None,
    document_type: Optional[str] = None,
    error_category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(TaskFeedback, Tenant.name.label("tenant_name"))
        .outerjoin(Tenant, TaskFeedback.tenant_id == Tenant.id)
        .where(TaskFeedback.status.in_(["ACCEPTED", "RESOLVED"]))
    )
    if tenant_id:
        stmt = stmt.where(TaskFeedback.tenant_id == tenant_id)
    if document_type:
        stmt = stmt.where(TaskFeedback.document_type == document_type)
    if error_category:
        stmt = stmt.where(TaskFeedback.error_category == error_category)
    rows = (
        await db.execute(
            stmt.order_by(TaskFeedback.created_at.desc()).limit(200)
        )
    ).all()
    if field:
        rows = [(fb, name) for fb, name in rows if field in (fb.diff_fields or [])]

    def _field_diffs(fb: TaskFeedback) -> List[dict]:
        """字段级差异：expected=客户改正值（正确答案），actual=系统原值。

        只保留客户真正修改过的叶子字段，单条上限 50 条，且不返回
        original_result/corrected_result 原文，保证列表接口轻量。
        """
        try:
            rows_ = build_field_diff_rows(fb.corrected_result or {}, fb.original_result or {})
        except Exception:
            return []
        diffs = [row for row in rows_ if not row.get("is_match")]
        return [
            {"field": row["field"], "expected": row["expected"], "actual": row["actual"]}
            for row in diffs[:50]
        ]

    return {"code": 0, "data": [{
        "id": fb.id,
        "task_id": fb.task_id,
        "tenant_id": fb.tenant_id,
        "tenant_name": tenant_name or fb.tenant_id,
        "status": fb.status,
        "diff_fields": fb.diff_fields or [],
        "field_diffs": _field_diffs(fb),
        "error_category": fb.error_category,
        "document_type": fb.document_type or "GENERAL",
        "review_comment": fb.review_comment,
        "created_at": fb.created_at.isoformat(),
    } for fb, tenant_name in rows]}


@router.post("/prompts/{prompt_id}/evaluate", summary="使用候选提示词运行回归")
async def evaluate_prompt_candidate(prompt_id: str, db: AsyncSession = Depends(get_db)):
    item = (await db.execute(select(PromptVersion).where(PromptVersion.id == prompt_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="提示词版本不存在")
    PromptService.validate_template(item.content)
    active = await _ensure_prompt_seed(db)
    baseline = await _run_layered_evaluation(
        db,
        prompt_template=active.content,
        prompt_version_id=active.id,
    )
    # 将 baseline 回归结果回写到当前 ACTIVE 版本。种子版本 builtin-v1 初始
    # evaluation_run_id 为 NULL，若无回写，候选切走后将永远无法通过启用门禁
    # 回滚回来。baseline 是本次刚跑出的最新 run，可直接覆盖更旧的指向；
    # 在用版本状态必须维持 ACTIVE，不受 VALIDATED 门禁影响。
    baseline_run_id = (baseline.get("holdout") or {}).get("evaluation_run_id") or (baseline.get("training") or {}).get("evaluation_run_id")
    if baseline_run_id and active.id != item.id and active.evaluation_run_id != baseline_run_id:
        active.evaluation_run_id = baseline_run_id
        active.status = "ACTIVE"
    result = await _run_layered_evaluation(
        db,
        prompt_template=item.content,
        prompt_version_id=item.id,
    )
    item.evaluation_run_id = (result.get("holdout") or {}).get("evaluation_run_id") or (result.get("training") or {}).get("evaluation_run_id")
    no_regression = (
        result.get("can_release")
        and result.get("overall_accuracy_percent", 0) >= baseline.get("overall_accuracy_percent", 0)
        and result.get("critical_regressions_count", 0) <= baseline.get("critical_regressions_count", 0)
    )
    item.status = "VALIDATED" if no_regression else "FAILED"
    await db.commit()
    return {
        "code": 0,
        "message": "候选提示词 A/B 回归完成",
        "data": {
            **result,
            "baseline": baseline,
            "candidate": result,
            "accuracy_delta": round(
                result.get("overall_accuracy_percent", 0) - baseline.get("overall_accuracy_percent", 0),
                1,
            ),
            "no_regression": bool(no_regression),
        },
    }


@router.post("/prompts/{prompt_id}/activate", summary="启用已通过回归的提示词或回滚")
async def activate_prompt_version(prompt_id: str, db: AsyncSession = Depends(get_db)):
    item = (await db.execute(select(PromptVersion).where(PromptVersion.id == prompt_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="提示词版本不存在")
    # 启用门禁规则（统筹 A/B 验证与回滚）：
    # 1. DRAFT / FAILED 候选：禁止启用，必须先通过 A/B 回归（状态 VALIDATED）。
    # 2. VALIDATED 候选：必须绑定一次 can_release 的回归 run 才能启用。
    # 3. ARCHIVED 历史版本（曾是 VALIDATED 后被切走归档，或跑过 baseline 的
    #    builtin-v1）：只要存在与其绑定的历史回归 run 即视为可信，允许回滚启用。
    # 4. 当前 ACTIVE 版本重复启用：幂等放行。
    if item.status == "ACTIVE":
        return {"code": 0, "message": f"提示词 {item.version_tag} 已是生产版本", "data": _prompt_payload(item)}
    run = None
    if item.evaluation_run_id:
        run = (
            await db.execute(select(EvaluationRun).where(EvaluationRun.id == item.evaluation_run_id))
        ).scalar_one_or_none()
    if item.status not in ("VALIDATED", "ARCHIVED"):
        raise HTTPException(
            status_code=409,
            detail="必须先通过 A/B 回归验证（状态 VALIDATED）才能启用",
        )
    if item.status == "VALIDATED":
        if not run or not run.can_release or run.prompt_version_id != item.id:
            raise HTTPException(status_code=409, detail="该提示词尚未通过与其绑定的金标回归，禁止启用")
    else:  # ARCHIVED 回滚：历史上跑过绑定回归即视为可信
        if not run or run.prompt_version_id != item.id:
            raise HTTPException(status_code=409, detail="该历史版本没有可信的回归记录，禁止回滚启用")
    await PromptService.activate(db, item)
    item.activated_at = utc_now()
    await db.commit()
    return {"code": 0, "message": f"提示词 {item.version_tag} 已启用", "data": _prompt_payload(item)}


@router.get("/evaluation/runs", summary="查询回归运行历史")
async def list_evaluation_runs(db: AsyncSession = Depends(get_db)):
    items = (
        await db.execute(select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(50))
    ).scalars().all()
    return {"code": 0, "data": [{
        "id": item.id,
        "prompt_version_id": item.prompt_version_id,
        "status": item.status,
        "model_name": item.model_name,
        "overall_accuracy": float(item.overall_accuracy or 0),
        "total_cases": item.total_cases,
        "passed_cases": item.passed_cases,
        "critical_regressions": item.critical_regressions,
        "can_release": item.can_release,
        "configuration_snapshot": item.configuration_snapshot,
        "case_results": item.case_results,
        "started_at": item.started_at.isoformat(),
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    } for item in items]}


@router.get("/benchmarks-export.jsonl", summary="导出标准答案 JSONL")
async def export_benchmarks_jsonl(db: AsyncSession = Depends(get_db)):
    cases = (
        await db.execute(
            select(BenchmarkCase)
            .where(
                BenchmarkCase.verification_status == "VERIFIED",
                BenchmarkCase.dataset_role == "TRAIN",
            )
            .order_by(BenchmarkCase.created_at.asc())
        )
    ).scalars().all()
    lines = []
    for case in cases:
        lines.append(json.dumps({
            "id": case.id,
            "feedback_id": case.feedback_id,
            "document_type": case.doc_type,
            "dataset_role": "TRAIN",
            "title": case.title,
            "input_text": case.input_text,
            "source_files": [Path(path).name for path in (case.source_files or [])],
            "source_hashes": case.source_hashes or {},
            "ground_truth": case.ground_truth,
            "weight": case.weight,
            "is_active": case.is_active,
        }, ensure_ascii=False) + "\n")
    body = "".join(lines).encode("utf-8")
    return StreamingResponse(
        iter([body]),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=benchmark.jsonl"},
    )
