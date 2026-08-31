import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
import uuid
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.task import EmailTask
from app.models.tenant import Tenant, ApiKey
from app.schemas.task import SkillV3InputPayload
from app.core.skill_runner import default_skill_runner
from app.core.normalizer import default_normalizer
from app.core.parser import process_uploaded_files
from app.core.validator import default_validator
from app.services.billing_service import BillingService
from app.services.few_shot_service import FewShotService
from app.services.prompt_service import PromptService
from app.services.vision_service import VisionBudget, VisionService
from app.services.webhook_dispatcher import dispatch_webhook

logger = logging.getLogger(__name__)


EMPTY_EXTRACTION_MESSAGE = (
    "模型返回空提取结果。可能原因：原始文件或正文未解析出有效内容、模型未识别出货代业务字段，"
    "或提示词与当前输入不匹配。请确认文件内容清晰且只包含一个业务案例后重试。"
)


class EmptyExtractionResultError(ValueError):
    """Raised when normalization only produced the allowed GoodsType default."""


def _has_meaningful_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_meaningful_value(item) for item in value)
    return True


def utc_now():
    return datetime.now(timezone.utc)


class ExtractionService:
    @staticmethod
    def ensure_meaningful_result(result: Dict[str, Any]) -> None:
        """Reject an all-empty result whose only value is the derived GoodsType=S default."""
        meaningful = {
            key: value
            for key, value in (result or {}).items()
            if key != "GoodsType" and _has_meaningful_value(value)
        }
        goods_type = (result or {}).get("GoodsType")
        if not meaningful and goods_type in (None, "", "S"):
            raise EmptyExtractionResultError(EMPTY_EXTRACTION_MESSAGE)

    @staticmethod
    async def process_task(
        task_id: str,
        tenant_secret: Optional[str] = None,
        lease_owner: Optional[str] = None,
    ):
        """
        Executes the entire extraction pipeline for an email task.
        """
        start_time = time.time()
        worker_id = lease_owner or f"local:{uuid.uuid4().hex}"
        claim_time = utc_now()
        lease_expires_at = claim_time + timedelta(seconds=settings.TASK_LEASE_SECONDS)
        logger.info(f"Starting processing for task: {task_id}")

        async with AsyncSessionLocal() as db:
            claim = await db.execute(
                update(EmailTask)
                .where(
                    EmailTask.id == task_id,
                    or_(
                        EmailTask.status == "PENDING",
                        and_(
                            EmailTask.status == "PROCESSING",
                            or_(
                                EmailTask.lease_expires_at.is_(None),
                                EmailTask.lease_expires_at < claim_time,
                            ),
                        ),
                    ),
                )
                .values(
                    status="PROCESSING",
                    started_at=claim_time,
                    error_message=None,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=EmailTask.attempt_count + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                await db.rollback()
                logger.info("Task %s was already claimed or is no longer pending", task_id)
                return
            await db.commit()

            task = (await db.execute(select(EmailTask).where(EmailTask.id == task_id))).scalar_one()
            tenant_active = (
                await db.execute(select(Tenant.is_active).where(Tenant.id == task.tenant_id))
            ).scalar_one_or_none()
            if tenant_active is not True:
                task.status = "FAILED"
                task.error_message = "Tenant is inactive or no longer exists"
                task.completed_at = utc_now()
                task.lease_owner = None
                task.lease_expires_at = None
                await BillingService.release_task_reservation(db, task.tenant_id, task.id)
                await db.commit()
                return

            tenant_id = task.tenant_id
            callback_url = task.callback_url
            input_type = task.input_type
            raw_input_json = task.raw_input_json
            file_paths_str = task.file_paths
            mail_subject = task.mail_subject

            # If tenant_secret wasn't passed, look it up from API key
            if not tenant_secret:
                key_stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
                if task.api_key_id:
                    key_stmt = key_stmt.where(ApiKey.id == task.api_key_id)
                else:
                    key_stmt = key_stmt.where(ApiKey.is_active.is_(True))
                key_res = await db.execute(key_stmt)
                active_key = key_res.scalars().first()
                tenant_secret = active_key.api_secret if active_key else None

        billing_completed = False

        # Step 1: Prepare Payload
        try:
            # Development Celery workers are separate processes, so refresh all
            # database-backed model settings before both text and file tasks.
            # Production uses immutable deployment environment and this is a no-op.
            await VisionService.refresh_runtime_settings()
            if input_type == "FILE" and file_paths_str:
                vision_budget = VisionBudget(settings.VISION_MAX_IMAGES_PER_TASK)
                stored_paths = json.loads(file_paths_str)
                if not isinstance(stored_paths, list) or len(stored_paths) > settings.MAX_UPLOAD_FILES:
                    raise ValueError("Invalid stored upload path list")
                upload_root = settings.uploads_path.resolve()
                file_paths = []
                for stored_path in stored_paths:
                    file_path = Path(stored_path).resolve()
                    if not file_path.is_relative_to(upload_root) or not file_path.is_file():
                        raise ValueError("Stored upload path is missing or outside the upload directory")
                    file_paths.append(file_path)
                payload = await asyncio.to_thread(
                    process_uploaded_files,
                    file_paths=file_paths,
                    subject=mail_subject or "",
                    body="",
                    temp_dir=settings.uploads_path,
                    vision_budget=vision_budget,
                )
            else:
                raw_dict = json.loads(raw_input_json or "{}")
                payload = SkillV3InputPayload(**raw_dict)


            # Step 2: Extract Draft JSON using SenseTime LLM with dynamic Few-Shot injection
            few_shot_snippet = ""
            prompt_template = None
            try:
                async with AsyncSessionLocal() as db_fs:
                    few_shot_snippet = await FewShotService.build_few_shot_prompt_section(
                        db_fs,
                        tenant_id=tenant_id,
                        doc_type=FewShotService.detect_document_type(payload),
                    )
                    active_prompt = await PromptService.get_active(db_fs)
                    prompt_template = active_prompt.content if active_prompt else None
            except Exception as fs_err:
                logger.debug("FewShot prompt snippet loading skipped: %s", fs_err)

            draft_json = await default_skill_runner.extract_draft_json(
                payload,
                few_shot_snippet=few_shot_snippet,
                prompt_template=prompt_template,
            )

            # Step 3: Normalize with Skill V3 Rules
            final_v3_json = default_normalizer.normalize(draft_json)
            is_valid, validation_errors = default_validator.validate(final_v3_json)
            if not is_valid:
                details = "; ".join(validation_errors[:5])
                raise ValueError(f"Normalized output failed V3 schema validation: {details}")
            ExtractionService.ensure_meaningful_result(final_v3_json)

            # Step 4: Record Success & Deduct Balance
            duration_ms = int((time.time() - start_time) * 1000)

            async with AsyncSessionLocal() as db:
                task_stmt = select(EmailTask).where(EmailTask.id == task_id)
                task_res = await db.execute(task_stmt)
                current_task = task_res.scalar_one()
                if current_task.status != "PROCESSING" or current_task.lease_owner != worker_id:
                    logger.warning("Task %s lease is no longer owned by %s", task_id, worker_id)
                    await db.rollback()
                    return

                current_task.status = "SUCCESS"
                current_task.result_json = json.dumps(final_v3_json, ensure_ascii=False)
                current_task.duration_ms = duration_ms
                current_task.completed_at = utc_now()
                current_task.lease_owner = None
                current_task.lease_expires_at = None
                await db.flush()

                # Billing commits the result and deduction in the same database transaction.
                billing_tx = await BillingService.deduct_for_task_success(db, tenant_id, task_id)
                if billing_tx is None:
                    raise RuntimeError("Insufficient balance or duplicate billing prevented task finalization")
                billing_completed = True
                await db.refresh(current_task)

                # Send Webhook notification
                if callback_url:
                    try:
                        webhook_data = {
                            "event": "task.completed",
                            "task_id": task_id,
                            "tenant_id": tenant_id,
                            "status": "SUCCESS",
                            "duration_ms": duration_ms,
                            "charged_amount": float(current_task.charged_amount),
                            "data": final_v3_json,
                            "error": None,
                            "timestamp": int(time.time() * 1000),
                        }
                        current_task.callback_status = await dispatch_webhook(
                            db=db,
                            task_id=task_id,
                            callback_url=callback_url,
                            tenant_secret=tenant_secret,
                            payload=webhook_data,
                        )
                        await db.commit()
                    except Exception as webhook_error:
                        await db.rollback()
                        logger.error("Webhook finalization failed for task %s: %s", task_id, webhook_error)
                        await db.execute(
                            update(EmailTask)
                            .where(EmailTask.id == task_id)
                            .values(callback_status="FAILED")
                        )
                        await db.commit()

            logger.info(f"Task {task_id} completed successfully in {duration_ms}ms")

        except asyncio.CancelledError:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(EmailTask)
                    .where(
                        EmailTask.id == task_id,
                        EmailTask.status == "PROCESSING",
                        EmailTask.lease_owner == worker_id,
                    )
                    .values(
                        status="PENDING",
                        started_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_dispatched_at=None,
                    )
                )
                await db.commit()
            raise
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            err_msg = str(e)[:2000]
            logger.error(f"Task {task_id} failed after {duration_ms}ms: {err_msg}", exc_info=True)

            # Once the atomic success+charge transaction committed, later bookkeeping
            # failures must not rewrite the task as uncharged/failed.
            if billing_completed:
                return

            async with AsyncSessionLocal() as db:
                task_stmt = select(EmailTask).where(EmailTask.id == task_id)
                task_res = await db.execute(task_stmt)
                current_task = task_res.scalar_one_or_none()
                if current_task:
                    if current_task.lease_owner != worker_id:
                        logger.warning("Ignoring stale failure from former task lease owner %s", worker_id)
                        await db.rollback()
                        return
                    current_task.status = "FAILED"
                    current_task.error_message = err_msg
                    current_task.duration_ms = duration_ms
                    current_task.completed_at = utc_now()
                    current_task.lease_owner = None
                    current_task.lease_expires_at = None
                    if not current_task.is_charged:
                        current_task.charged_amount = Decimal("0.0000")
                    await BillingService.release_task_reservation(db, tenant_id, task_id)
                    await db.commit()

                    # Send Webhook error notification
                    if callback_url:
                        webhook_data = {
                            "event": "task.failed",
                            "task_id": task_id,
                            "tenant_id": tenant_id,
                            "status": "FAILED",
                            "duration_ms": duration_ms,
                            "charged_amount": 0.0,
                            "data": None,
                            "error": err_msg,
                            "timestamp": int(time.time() * 1000),
                        }
                        current_task.callback_status = await dispatch_webhook(
                            db=db,
                            task_id=task_id,
                            callback_url=callback_url,
                            tenant_secret=tenant_secret,
                            payload=webhook_data,
                        )
                        await db.commit()

    @classmethod
    async def prepare_mail_payload(
        cls,
        subject: str = "",
        body: str = "",
        attachment_paths: Optional[List[str]] = None,
        parser_stage_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> SkillV3InputPayload:
        """Parse files and run OCR once, returning a reusable model input payload."""
        if attachment_paths:
            file_paths = [Path(p) for p in attachment_paths if Path(p).exists()]
            if file_paths:
                vision_budget = VisionBudget(settings.VISION_MAX_IMAGES_PER_TASK)
                return await asyncio.to_thread(
                    process_uploaded_files,
                    file_paths=file_paths,
                    subject=subject,
                    body=body,
                    temp_dir=settings.uploads_path,
                    vision_budget=vision_budget,
                    stage_callback=parser_stage_callback,
                )
        return SkillV3InputPayload(mail_subject=subject, mail_body=body, attachments=[])

    @classmethod
    async def extract_prepared_payload(
        cls,
        db: AsyncSession,
        payload: SkillV3InputPayload,
        tenant_id: Optional[str] = None,
        prompt_template: Optional[str] = None,
        extra_few_shot_snippet: str = "",
        model_progress_callback: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[None]]
        ] = None,
    ) -> Dict[str, Any]:
        """Run prompt extraction against an already parsed/OCR'd payload."""
        if prompt_template is None:
            try:
                active_prompt = await PromptService.get_active(db)
                prompt_template = active_prompt.content if active_prompt else None
            except Exception as prompt_err:
                logger.debug("Active prompt loading skipped: %s", prompt_err)

        few_shot_snippet = ""
        try:
            few_shot_snippet = await FewShotService.build_few_shot_prompt_section(
                db,
                tenant_id=tenant_id,
                doc_type=FewShotService.detect_document_type(payload),
            )
        except Exception as fs_err:
            logger.debug("FewShot prompt snippet loading skipped: %s", fs_err)
        if extra_few_shot_snippet:
            few_shot_snippet = "\n\n".join(
                part for part in (few_shot_snippet, extra_few_shot_snippet) if part
            )

        draft_json = await default_skill_runner.extract_draft_json(
            payload,
            few_shot_snippet=few_shot_snippet,
            prompt_template=prompt_template,
            progress_callback=model_progress_callback,
        )
        final_v3_json = default_normalizer.normalize(draft_json)
        cls.ensure_meaningful_result(final_v3_json)
        return final_v3_json

    @classmethod
    async def extract_mail_content(
        cls,
        db: AsyncSession,
        subject: str = "",
        body: str = "",
        attachment_paths: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
        prompt_template: Optional[str] = None,
        extra_few_shot_snippet: str = "",
        parser_stage_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        model_progress_callback: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[None]]
        ] = None,
        prepared_payload: Optional[SkillV3InputPayload] = None,
    ) -> Dict[str, Any]:
        """Parse/OCR and extract content in one call for normal application flows."""
        payload = prepared_payload
        if payload is None:
            payload = await cls.prepare_mail_payload(
                subject=subject,
                body=body,
                attachment_paths=attachment_paths,
                parser_stage_callback=parser_stage_callback,
            )
        return await cls.extract_prepared_payload(
            db=db,
            payload=payload,
            tenant_id=tenant_id,
            prompt_template=prompt_template,
            extra_few_shot_snippet=extra_few_shot_snippet,
            model_progress_callback=model_progress_callback,
        )
