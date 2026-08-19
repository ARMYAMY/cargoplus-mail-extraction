import asyncio
from datetime import datetime, timedelta, timezone
import logging
import random
import json
import time
from typing import Optional

from sqlalchemy import or_, select, update

from app.celery_app import celery_app
from app.config import settings
from app.core.limits import MAX_TENANT_CONCURRENCY, MIN_TENANT_CONCURRENCY
from app.core.redis_client import get_redis
from app.database import AsyncSessionLocal, engine
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.models.tenant import ApiKey
from app.services.billing_service import BillingService
from app.services.extraction_service import ExtractionService
from app.services.webhook_service import send_webhook_notification

logger = logging.getLogger(__name__)

ACQUIRE_SEMAPHORE = """
-- A sorted set makes tenant slots crash-safe. Each worker owns a token whose
-- score is its expiry time; dead workers therefore stop consuming capacity.
-- Delete the legacy integer counter during a rolling upgrade.
local key_type = redis.call('type', KEYS[1])['ok']
if key_type ~= 'none' and key_type ~= 'zset' then
  redis.call('del', KEYS[1])
end
local now_parts = redis.call('time')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local token = ARGV[3]
redis.call('zremrangebyscore', KEYS[1], '-inf', now)
if redis.call('zscore', KEYS[1], token) then
  redis.call('zadd', KEYS[1], now + ttl, token)
  redis.call('expire', KEYS[1], math.ceil(ttl))
  return 1
end
if redis.call('zcard', KEYS[1]) >= limit then return 0 end
redis.call('zadd', KEYS[1], now + ttl, token)
redis.call('expire', KEYS[1], math.ceil(ttl))
return 1
"""

RELEASE_SEMAPHORE = """
if redis.call('type', KEYS[1])['ok'] ~= 'zset' then return 0 end
local removed = redis.call('zrem', KEYS[1], ARGV[1])
if redis.call('zcard', KEYS[1]) == 0 then redis.call('del', KEYS[1]) end
return removed
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redis():
    return get_redis(decode_responses=True)


async def _run_and_dispose(awaitable):
    try:
        return await awaitable
    finally:
        # Celery prefork workers call asyncio.run per task. Asyncpg connections
        # cannot be reused by the next task's event loop.
        await engine.dispose()


def run_async(awaitable):
    return asyncio.run(_run_and_dispose(awaitable))


async def _get_task_context(task_id: str) -> Optional[tuple[str, int]]:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(EmailTask.tenant_id, Tenant.max_concurrency)
                .join(Tenant, Tenant.id == EmailTask.tenant_id)
                .where(
                    EmailTask.id == task_id,
                    EmailTask.status.in_(["PENDING", "PROCESSING"]),
                    Tenant.is_active.is_(True),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        tenant_id, configured_limit = row
        limit = max(
            MIN_TENANT_CONCURRENCY,
            min(int(configured_limit), MAX_TENANT_CONCURRENCY, settings.WORKER_CONCURRENCY),
        )
        return tenant_id, limit


async def _mark_timeout(task_id: str, tenant_id: str) -> None:
    async with AsyncSessionLocal() as db:
        task = (
            await db.execute(
                select(EmailTask).where(
                    EmailTask.id == task_id,
                    EmailTask.tenant_id == tenant_id,
                    EmailTask.status.in_(["PENDING", "PROCESSING"]),
                )
            )
        ).scalar_one_or_none()
        if task is None:
            return
        await BillingService.release_task_reservation(db, tenant_id, task_id)
        task.status = "FAILED"
        task.error_message = f"Task timed out after {settings.TASK_TIMEOUT_SECONDS} seconds"
        task.completed_at = utc_now()
        task.lease_owner = None
        task.lease_expires_at = None
        await db.commit()


async def _run_with_timeout(task_id: str, tenant_id: str, worker_id: str) -> None:
    try:
        await asyncio.wait_for(
            ExtractionService.process_task(task_id, lease_owner=worker_id),
            timeout=settings.TASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Task %s timed out", task_id)
        await _mark_timeout(task_id, tenant_id)


@celery_app.task(
    bind=True,
    name="cargoplus.process_email_task",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=None,
)
def process_email_task(self, task_id: str) -> None:
    context = run_async(_get_task_context(task_id))
    if context is None:
        return
    tenant_id, tenant_limit = context
    semaphore_key = f"cargoplus:tenant:{tenant_id}:active"
    redis_client = _redis()
    semaphore_ttl = settings.TASK_TIMEOUT_SECONDS + 120
    ownership_token = str(self.request.id or task_id)
    acquired = bool(
        redis_client.eval(
            ACQUIRE_SEMAPHORE,
            1,
            semaphore_key,
            tenant_limit,
            semaphore_ttl,
            ownership_token,
        )
    )
    if not acquired:
        redis_client.close()
        # Capacity contention is expected. A wider jitter avoids hammering Redis
        # when a large batch targets one tenant.
        raise self.retry(countdown=random.uniform(5.0, 15.0))

    try:
        run_async(_run_with_timeout(task_id, tenant_id, self.request.id))
    finally:
        try:
            redis_client.eval(RELEASE_SEMAPHORE, 1, semaphore_key, ownership_token)
        finally:
            redis_client.close()


async def _deliver_task_webhook(task_id: str) -> None:
    async with AsyncSessionLocal() as db:
        task = (await db.execute(select(EmailTask).where(EmailTask.id == task_id))).scalar_one_or_none()
        if task is None or not task.callback_url or task.status not in {"SUCCESS", "FAILED"}:
            return
        key_stmt = select(ApiKey).where(ApiKey.tenant_id == task.tenant_id)
        if task.api_key_id:
            key_stmt = key_stmt.where(ApiKey.id == task.api_key_id)
        else:
            key_stmt = key_stmt.where(ApiKey.is_active.is_(True))
        api_key = (await db.execute(key_stmt)).scalars().first()
        if api_key is None:
            await db.execute(
                update(EmailTask).where(EmailTask.id == task_id).values(callback_status="FAILED")
            )
            await db.commit()
            return

        payload = {
            "event": "task.completed" if task.status == "SUCCESS" else "task.failed",
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "status": task.status,
            "duration_ms": task.duration_ms,
            "charged_amount": float(task.charged_amount),
            "data": json.loads(task.result_json or "{}") if task.status == "SUCCESS" else None,
            "error": task.error_message if task.status == "FAILED" else None,
            "timestamp": int(time.time() * 1000),
        }
        delivered = await send_webhook_notification(
            db=db,
            task_id=task.id,
            callback_url=task.callback_url,
            secret=api_key.api_secret,
            payload_dict=payload,
        )
        await db.execute(
            update(EmailTask)
            .where(EmailTask.id == task_id)
            .values(callback_status="SUCCESS" if delivered else "FAILED")
        )
        await db.commit()


@celery_app.task(
    name="cargoplus.deliver_task_webhook",
    acks_late=True,
    reject_on_worker_lost=True,
)
def deliver_task_webhook(task_id: str) -> None:
    awaitable = _deliver_task_webhook(task_id)
    try:
        run_async(awaitable)
    finally:
        # Also close the coroutine when run_async is mocked by a unit test or
        # instrumentation hook and therefore does not consume it.
        awaitable.close()


async def _prepare_recovery_batch() -> list[str]:
    now = utc_now()
    dispatch_before = now - timedelta(seconds=settings.TASK_DISPATCH_STALE_SECONDS)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(EmailTask)
            .where(
                EmailTask.status == "PROCESSING",
                or_(EmailTask.lease_expires_at.is_(None), EmailTask.lease_expires_at < now),
            )
            .values(status="PENDING", lease_owner=None, lease_expires_at=None)
        )
        candidates = (
            await db.execute(
                select(EmailTask.id)
                .where(
                    EmailTask.status == "PENDING",
                    or_(
                        EmailTask.last_dispatched_at.is_(None),
                        EmailTask.last_dispatched_at < dispatch_before,
                    ),
                )
                .order_by(EmailTask.created_at)
                .limit(settings.TASK_RECOVERY_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        if candidates:
            await db.execute(
                update(EmailTask)
                .where(EmailTask.id.in_(candidates))
                .values(last_dispatched_at=now)
            )
        await db.commit()
        return list(candidates)


@celery_app.task(name="cargoplus.recover_stale_tasks", ignore_result=True)
def recover_stale_tasks() -> int:
    redis_client = _redis()
    try:
        lock_acquired = redis_client.set(
            "cargoplus:beat:recover-stale-tasks",
            str(time.time()),
            nx=True,
            ex=settings.BEAT_LOCK_TTL_SECONDS,
        )
        if lock_acquired:
            redis_client.set(
                "cargoplus:beat:last-recovery-tick",
                str(time.time()),
                ex=max(settings.TASK_RECOVERY_INTERVAL_SECONDS * 10, 300),
            )
    finally:
        redis_client.close()
    if not lock_acquired:
        logger.info("Skipped duplicate recovery schedule invocation")
        return 0
    task_ids = run_async(_prepare_recovery_batch())
    for task_id in task_ids:
        process_email_task.apply_async(args=[task_id], queue=settings.CELERY_QUEUE_NAME)
    if task_ids:
        logger.info("Recovered and dispatched %s task(s)", len(task_ids))
    return len(task_ids)
