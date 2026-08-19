import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import logging
from typing import Dict, Optional, Tuple
from sqlalchemy import select, update
from app.config import settings
from app.core.limits import MAX_TENANT_CONCURRENCY, MIN_TENANT_CONCURRENCY
from app.database import AsyncSessionLocal
from app.models.tenant import ApiKey, Tenant
from app.models.task import EmailTask
from app.services.billing_service import BillingService
from app.services.extraction_service import ExtractionService

logger = logging.getLogger(__name__)


class TaskQueueManager:
    def __init__(self):
        self._queue: asyncio.Queue[Tuple[str, str, Optional[str]]] = asyncio.Queue()
        self._active_tenant_counts: Dict[str, int] = defaultdict(int)
        self._worker_tasks: list[asyncio.Task] = []
        self._is_running = False
        self._semaphore = asyncio.Semaphore(settings.WORKER_CONCURRENCY)

    async def start(self):
        """Starts worker pool on application startup and recovers interrupted tasks."""
        if self._is_running:
            return
        self._is_running = True
        logger.info(f"Starting TaskQueueManager with max {settings.WORKER_CONCURRENCY} workers")

        # Recover database state before workers consume, otherwise a recovered PROCESSING
        # task can be dequeued before its reset-to-PENDING transaction is committed.
        await self._recover_uncompleted_tasks()
        for i in range(settings.WORKER_CONCURRENCY):
            worker = asyncio.create_task(self._worker_loop(worker_id=i))
            self._worker_tasks.append(worker)

    async def _recover_uncompleted_tasks(self):
        """Scans database for PENDING or PROCESSING tasks from prior runs and re-enqueues them."""
        try:
            recovered = []
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(EmailTask)
                    .where(EmailTask.status == "PROCESSING")
                    .values(status="PENDING", started_at=None)
                )
                await db.commit()

                stmt = select(EmailTask).where(EmailTask.status == "PENDING")
                res = await db.execute(stmt)
                tasks = res.scalars().all()

                if not tasks:
                    return

                logger.info(f"Recovering {len(tasks)} uncompleted tasks from database...")
                for task in tasks:
                    key_stmt = select(ApiKey).where(ApiKey.tenant_id == task.tenant_id, ApiKey.is_active == True)
                    key_res = await db.execute(key_stmt)
                    key = key_res.scalars().first()
                    secret = key.api_secret if key else None
                    recovered.append((task.id, task.tenant_id, secret))

            for task_id, tenant_id, secret in recovered:
                await self.enqueue(task_id, tenant_id, secret)
            logger.info("Successfully re-enqueued %s recovered tasks.", len(recovered))
        except Exception as e:
            logger.error(f"Error during uncompleted task recovery: {e}", exc_info=True)

    async def stop(self):
        """Stops worker pool gracefully."""
        self._is_running = False
        for worker in self._worker_tasks:
            worker.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        logger.info("TaskQueueManager stopped.")

    async def enqueue(self, task_id: str, tenant_id: str, tenant_secret: Optional[str] = None):
        """Pushes a new task into the processing queue."""
        await self._queue.put((task_id, tenant_id, tenant_secret))
        logger.info(f"Enqueued task {task_id} for tenant {tenant_id}. Queue size: {self._queue.qsize()}")

    async def _get_tenant_max_concurrency(self, tenant_id: str) -> int:
        try:
            async with AsyncSessionLocal() as db:
                stmt = select(Tenant.max_concurrency).where(Tenant.id == tenant_id)
                res = await db.execute(stmt)
                val = res.scalar_one_or_none()
                configured_limit = val if val is not None else settings.DEFAULT_TENANT_CONCURRENCY
                return max(
                    MIN_TENANT_CONCURRENCY,
                    min(int(configured_limit), MAX_TENANT_CONCURRENCY, settings.WORKER_CONCURRENCY),
                )
        except Exception:
            return max(
                MIN_TENANT_CONCURRENCY,
                min(
                    settings.DEFAULT_TENANT_CONCURRENCY,
                    MAX_TENANT_CONCURRENCY,
                    settings.WORKER_CONCURRENCY,
                ),
            )

    async def _worker_loop(self, worker_id: int):
        while self._is_running:
            try:
                task_id, tenant_id, tenant_secret = await self._queue.get()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error getting task from queue: {e}")
                continue

            try:
                # Check tenant-level concurrency limit
                max_conc = await self._get_tenant_max_concurrency(tenant_id)
                if self._active_tenant_counts[tenant_id] >= max_conc and self._is_running:
                    # Put the item at the back so one busy tenant cannot occupy every worker
                    # while unrelated tenants wait behind it.
                    await self._queue.put((task_id, tenant_id, tenant_secret))
                    await asyncio.sleep(0.05)
                    continue

                if not self._is_running:
                    break

                self._active_tenant_counts[tenant_id] += 1
                async with self._semaphore:
                    try:
                        await asyncio.wait_for(
                            ExtractionService.process_task(task_id, tenant_secret),
                            timeout=settings.TASK_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        logger.error(f"Task {task_id} timed out after {settings.TASK_TIMEOUT_SECONDS}s")
                        async with AsyncSessionLocal() as db:
                            await BillingService.release_task_reservation(db, tenant_id, task_id)
                            await db.execute(
                                update(EmailTask)
                                .where(
                                    EmailTask.id == task_id,
                                    EmailTask.status.in_(["PENDING", "PROCESSING"]),
                                )
                                .values(
                                    status="FAILED",
                                    error_message=f"Task timed out after {settings.TASK_TIMEOUT_SECONDS} seconds",
                                    completed_at=datetime.now(timezone.utc),
                                )
                            )
                            await db.commit()
                    except Exception as ex:
                        logger.error(f"Worker {worker_id} error on task {task_id}: {ex}", exc_info=True)
                    finally:
                        self._active_tenant_counts[tenant_id] = max(0, self._active_tenant_counts[tenant_id] - 1)

            finally:
                self._queue.task_done()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def active_tenants(self) -> Dict[str, int]:
        return {k: v for k, v in self._active_tenant_counts.items() if v > 0}

    async def health(self) -> bool:
        return True


class CeleryTaskQueueManager:
    """Thin API-side adapter for the durable Redis/Celery queue."""

    async def start(self) -> None:
        from app.core.redis_client import get_async_redis

        try:
            redis_client = get_async_redis()
            try:
                await redis_client.ping()
                await self._recover_uncompleted_tasks()
                logger.info("Celery task queue is ready")
            finally:
                await redis_client.aclose()
        except Exception as e:
            logger.warning(f"Redis not available during queue startup ({e}). Standalone mode active.")

    async def stop(self) -> None:
        return None

    async def enqueue(
        self,
        task_id: str,
        tenant_id: str,
        tenant_secret: Optional[str] = None,
    ) -> None:
        del tenant_id, tenant_secret  # Secrets never enter the message broker.
        from app.celery_tasks import process_email_task

        await asyncio.to_thread(
            process_email_task.apply_async,
            args=[task_id],
            queue=settings.CELERY_QUEUE_NAME,
        )
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(EmailTask)
                .where(EmailTask.id == task_id, EmailTask.status == "PENDING")
                .values(last_dispatched_at=datetime.now(timezone.utc))
            )
            await db.commit()

    async def _recover_uncompleted_tasks(self) -> None:
        from app.celery_tasks import recover_stale_tasks

        await asyncio.to_thread(
            recover_stale_tasks.apply_async,
            queue=settings.CELERY_QUEUE_NAME,
        )

    @property
    def queue_size(self) -> int:
        from app.core.redis_client import get_redis

        client = get_redis()
        try:
            return int(client.llen(settings.CELERY_QUEUE_NAME))
        except Exception:
            return -1
        finally:
            client.close()

    @property
    def active_tenants(self) -> Dict[str, int]:
        from app.core.redis_client import get_redis

        client = get_redis(decode_responses=True)
        active: Dict[str, int] = {}
        try:
            prefix = "cargoplus:tenant:"
            suffix = ":active"
            for key in client.scan_iter(match=f"{prefix}*{suffix}", count=100):
                tenant_id = key[len(prefix) : -len(suffix)]
                key_type = client.type(key)
                if key_type in {"zset", b"zset"}:
                    count = int(client.zcard(key))
                else:
                    # Compatibility with the old counter representation during
                    # a rolling deployment; the next acquire converts it.
                    count = int(client.get(key) or 0)
                if count > 0:
                    active[tenant_id] = count
            return active
        except Exception:
            return {}
        finally:
            client.close()

    async def health(self) -> bool:
        from app.core.redis_client import get_async_redis

        client = get_async_redis()
        try:
            return bool(await client.ping())
        except Exception:
            return False
        finally:
            await client.aclose()


task_queue = (
    TaskQueueManager()
    if settings.TASK_QUEUE_MODE == "local"
    else CeleryTaskQueueManager()
)
