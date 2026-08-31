import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, Optional

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.feedback import AdminJob

logger = logging.getLogger(__name__)
JobRunner = Callable[[str], Awaitable[None]]


class JobCancelled(Exception):
    pass


class AdminJobService:
    _tasks: Dict[str, asyncio.Task] = {}

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    @classmethod
    def schedule(cls, job_id: str, runner: JobRunner) -> None:
        current = cls._tasks.get(job_id)
        if current and not current.done():
            return
        task = asyncio.create_task(cls._run_guarded(job_id, runner))
        cls._tasks[job_id] = task
        task.add_done_callback(lambda _task: cls._tasks.pop(job_id, None))

    @classmethod
    async def _run_guarded(cls, job_id: str, runner: JobRunner) -> None:
        try:
            await runner(job_id)
        except JobCancelled:
            await cls.fail(job_id, "CANCELLED", "任务已由管理员取消", status="CANCELLED")
        except asyncio.CancelledError:
            async with AsyncSessionLocal() as db:
                cancel_requested = (
                    await db.execute(select(AdminJob.cancel_requested).where(AdminJob.id == job_id))
                ).scalar_one_or_none()
            if cancel_requested:
                await cls.fail(job_id, "CANCELLED", "任务已由管理员取消", status="CANCELLED")
            else:
                await cls.fail(job_id, "INTERRUPTED", "服务重载导致任务中断，可重新发起", status="FAILED")
            raise
        except httpx.ConnectError as exc:
            await cls.fail(
                job_id,
                "CONNECTION_FAILED",
                "无法连接模型服务；请检查 Base URL、DNS、代理及 8001 进程的外网访问权限",
            )
        except httpx.TimeoutException as exc:
            await cls.fail(job_id, "TIMEOUT", f"模型或评测请求超时: {exc}")
        except httpx.HTTPStatusError as exc:
            await cls.fail(job_id, "UPSTREAM_HTTP_ERROR", f"模型服务返回 HTTP {exc.response.status_code}: {exc}")
        except (ValueError, TypeError) as exc:
            message = str(exc)
            if (
                "没有返回正文" in message
                or "正文为空" in message
                or "模型返回空提取结果" in message
            ):
                code = "EMPTY_RESPONSE"
            else:
                code = "FORMAT_ERROR" if "JSON" in message or "格式" in message or "rules 数组" in message else "VALIDATION_FAILED"
            await cls.fail(job_id, code, message)
        except Exception as exc:
            logger.exception("Admin job %s failed", job_id)
            message = str(exc)
            code = "FORMAT_ERROR" if "JSON" in message or "格式" in message else "INTERNAL_ERROR"
            await cls.fail(job_id, code, message[:2000])

    @classmethod
    async def update(cls, job_id: str, **values) -> Optional[AdminJob]:
        async with AsyncSessionLocal() as db:
            item = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one_or_none()
            if not item:
                return None
            for key, value in values.items():
                setattr(item, key, value)
            item.updated_at = cls.now()
            await db.commit()
            await db.refresh(item)
            return item

    @classmethod
    async def fail(cls, job_id: str, code: str, message: str, status: str = "FAILED") -> None:
        await cls.update(
            job_id,
            status=status,
            phase=status,
            error_code=code,
            error_message=message,
            finished_at=cls.now(),
        )

    @classmethod
    async def check_cancelled(cls, job_id: str) -> None:
        async with AsyncSessionLocal() as db:
            flag = (
                await db.execute(select(AdminJob.cancel_requested).where(AdminJob.id == job_id))
            ).scalar_one_or_none()
        if flag:
            raise JobCancelled()

    @classmethod
    async def cancel(cls, job_id: str) -> bool:
        item = await cls.update(job_id, cancel_requested=True, phase="CANCELLING")
        task = cls._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        return item is not None

    @classmethod
    async def mark_interrupted_jobs(cls) -> None:
        """Hot reload cannot resume in-flight provider sockets; expose an actionable state."""
        async with AsyncSessionLocal() as db:
            items = (
                await db.execute(select(AdminJob).where(AdminJob.status.in_(["QUEUED", "RUNNING"])))
            ).scalars().all()
            for item in items:
                item.status = "FAILED"
                item.phase = "FAILED"
                item.error_code = "INTERRUPTED"
                item.error_message = "服务重载时任务被中断，请从该任务重新发起"
                item.finished_at = cls.now()
            await db.commit()


def job_payload(item: AdminJob) -> dict:
    return {
        "id": item.id,
        "job_type": item.job_type,
        "status": item.status,
        "phase": item.phase,
        "progress_current": item.progress_current,
        "progress_total": item.progress_total,
        "progress_percent": item.progress_percent,
        "input_payload": item.input_payload or {},
        "result": item.result or {},
        "stream_text": item.stream_text or "",
        "related_entity_id": item.related_entity_id,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "cancel_requested": item.cancel_requested,
        "created_at": item.created_at.isoformat(),
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        "updated_at": item.updated_at.isoformat(),
    }
