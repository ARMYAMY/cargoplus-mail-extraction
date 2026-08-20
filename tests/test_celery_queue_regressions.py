from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.celery_tasks import (
    _get_task_context,
    _mark_timeout,
    _prepare_recovery_batch,
    _run_with_timeout,
    process_email_task,
    recover_stale_tasks,
)
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.task import EmailTask
from app.models.tenant import ApiKey, Tenant
from app.services.auth_service import generate_api_key_and_secret
from app.services.queue_service import task_queue, TaskQueueManager, CeleryTaskQueueManager


async def create_tenant() -> tuple[str, str]:
    tenant_id = f"tenant_{uuid.uuid4().hex[:10]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
    async with AsyncSessionLocal() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name=f"Queue Tenant {tenant_id}",
                balance=Decimal("100.0000"),
                unit_price=Decimal("0.5000"),
                is_active=True,
            )
        )
        db.add(
            ApiKey(
                tenant_id=tenant_id,
                name="queue-test",
                key_prefix=prefix,
                key_hash=key_hash,
                api_secret=secret,
            )
        )
        await db.commit()
    return tenant_id, raw_key


@pytest.mark.asyncio
async def test_async_submission_idempotency_reserves_and_creates_only_once():
    await init_db()
    tenant_id, raw_key = await create_tenant()
    headers = {"Authorization": f"Bearer {raw_key}", "Idempotency-Key": uuid.uuid4().hex}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/extract/async", headers=headers, json={"mail_body": "cargo"})
        second = await client.post("/api/v1/extract/async", headers=headers, json={"mail_body": "cargo"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]

    async with AsyncSessionLocal() as db:
        task_count = (
            await db.execute(
                select(func.count(EmailTask.id)).where(EmailTask.tenant_id == tenant_id)
            )
        ).scalar_one()
        tenant = await db.get(Tenant, tenant_id)
        assert task_count == 1
        assert tenant.reserved_balance == Decimal("0.5000")


@pytest.mark.asyncio
async def test_tenant_pending_limit_returns_429(monkeypatch):
    await init_db()
    _, raw_key = await create_tenant()
    monkeypatch.setattr(settings, "MAX_TENANT_PENDING_TASKS", 1)
    headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/extract/async", headers=headers, json={"mail_body": "first"})
        second = await client.post("/api/v1/extract/async", headers=headers, json={"mail_body": "second"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "30"


@pytest.mark.asyncio
async def test_recovery_only_requeues_expired_processing_lease():
    await init_db()
    tenant_id, _ = await create_tenant()
    now = datetime.now(timezone.utc)
    expired_id = f"task_{uuid.uuid4().hex[:10]}"
    active_id = f"task_{uuid.uuid4().hex[:10]}"
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                EmailTask(
                    id=expired_id,
                    tenant_id=tenant_id,
                    status="PROCESSING",
                    lease_owner="dead-worker",
                    lease_expires_at=now - timedelta(seconds=1),
                ),
                EmailTask(
                    id=active_id,
                    tenant_id=tenant_id,
                    status="PROCESSING",
                    lease_owner="live-worker",
                    lease_expires_at=now + timedelta(minutes=5),
                ),
            ]
        )
        await db.commit()

    recovered = await _prepare_recovery_batch()
    assert expired_id in recovered
    assert active_id not in recovered

    async with AsyncSessionLocal() as db:
        expired = await db.get(EmailTask, expired_id)
        active = await db.get(EmailTask, active_id)
        assert expired.status == "PENDING"
        assert expired.lease_owner is None
        assert active.status == "PROCESSING"
        assert active.lease_owner == "live-worker"


@pytest.mark.asyncio
async def test_celery_task_context_and_timeout():
    await init_db()
    tenant_id, _ = await create_tenant()
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        db.add(EmailTask(id=task_id, tenant_id=tenant_id, status="PENDING"))
        await db.commit()

    # 1. _get_task_context
    ctx = await _get_task_context(task_id)
    assert ctx is not None
    assert ctx[0] == tenant_id
    assert ctx[1] >= 1

    # Non-existent task context
    ctx_none = await _get_task_context("non_existent_task_999")
    assert ctx_none is None

    # 2. _mark_timeout
    await _mark_timeout(task_id, tenant_id)
    async with AsyncSessionLocal() as db:
        t_obj = await db.get(EmailTask, task_id)
        assert t_obj.status == "FAILED"
        assert "timed out" in t_obj.error_message

    # Mark timeout on non-existent task
    await _mark_timeout("non_existent_task_999", tenant_id)


def test_celery_process_email_task_execution():
    # 1. process_email_task when context is None
    with patch("app.celery_tasks._get_task_context", return_value=None):
        process_email_task("fake_task_id")

    # 2. process_email_task normal execution with acquired semaphore
    mock_redis = MagicMock()
    mock_redis.eval.side_effect = [1, 0]  # acquire -> 1, release -> 0

    with patch("app.celery_tasks._get_task_context", return_value=("t_1", 5)), \
         patch("app.celery_tasks._redis", return_value=mock_redis), \
         patch("app.celery_tasks._run_with_timeout", new_callable=AsyncMock):
        process_email_task("task_123")
    acquire_call, release_call = mock_redis.eval.call_args_list
    assert acquire_call.args[-1]
    assert release_call.args[-1] == acquire_call.args[-1]

    # 3. recover_stale_tasks
    recovery_redis = MagicMock()
    recovery_redis.set.return_value = True
    with patch("app.celery_tasks._prepare_recovery_batch", return_value=["t1", "t2"]), \
         patch("app.celery_tasks.process_email_task.apply_async"), \
         patch("app.celery_tasks._redis", return_value=recovery_redis):
        recovered_count = recover_stale_tasks()
        assert recovered_count == 2


@pytest.mark.asyncio
async def test_celery_task_queue_manager_adapter():
    mgr = CeleryTaskQueueManager()

    # 1. health & start with mock redis
    mock_async_redis = AsyncMock()
    mock_async_redis.ping.return_value = True

    with patch("redis.asyncio.Redis.from_url", return_value=mock_async_redis), \
         patch("app.celery_tasks.recover_stale_tasks.apply_async"):
        await mgr.start()
        h_ok = await mgr.health()
        assert h_ok is True
        await mgr.stop()

    # 2. enqueue task
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        t_id, _ = await create_tenant()
        db.add(EmailTask(id=task_id, tenant_id=t_id, status="PENDING"))
        await db.commit()

    with patch("app.celery_tasks.process_email_task.apply_async"):
        await mgr.enqueue(task_id, t_id)

    # 3. queue_size & active_tenants
    mock_sync_redis = MagicMock()
    mock_sync_redis.llen.return_value = 4
    mock_sync_redis.scan_iter.return_value = ["cargoplus:tenant:tenant_001:active"]
    mock_sync_redis.type.return_value = "zset"
    mock_sync_redis.zcard.return_value = 2

    with patch("redis.Redis.from_url", return_value=mock_sync_redis):
        assert mgr.queue_size == 4
        active = mgr.active_tenants
        assert active.get("tenant_001") == 2

    # 4. Exception branches for queue_size and active_tenants
    mock_sync_err = MagicMock()
    mock_sync_err.llen.side_effect = Exception("Redis Error")
    mock_sync_err.scan_iter.side_effect = Exception("Redis Error")

    with patch("redis.Redis.from_url", return_value=mock_sync_err):
        assert mgr.queue_size == -1
        assert mgr.active_tenants == {}


@pytest.mark.asyncio
async def test_celery_dispatch_failure_keeps_fallback_context(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    mgr = CeleryTaskQueueManager()
    fallback = MagicMock(spec=TaskQueueManager)
    fallback.start = AsyncMock()
    fallback.enqueue = AsyncMock()

    with (
        patch("app.celery_tasks.process_email_task.apply_async", side_effect=RuntimeError("broker down")),
        patch("app.services.queue_service.TaskQueueManager", return_value=fallback),
    ):
        await mgr.enqueue("task_fallback", "tenant_fallback", "secret_fallback")

    fallback.start.assert_awaited_once()
    fallback.enqueue.assert_awaited_once_with(
        "task_fallback", "tenant_fallback", "secret_fallback"
    )
