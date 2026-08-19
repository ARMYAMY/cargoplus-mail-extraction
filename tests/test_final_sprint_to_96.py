import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings, Settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.main import app as main_app
from app.monitor import app as monitor_app


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_main_http_metrics_middleware_execution():
    transport = ASGITransport(app=main_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch.object(settings, "METRICS_ENABLED", True):
            # Test a normal route that triggers record_http_metrics middleware lines 139-153
            res = await client.get("/health/live")
            assert res.status_code == 200

            # Test an unmatched 404 route
            res_404 = await client.get("/unmatched_route_for_metrics")
            assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_monitor_metrics_with_database_records(tmp_path):
    now = datetime.now(timezone.utc)
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="MonitorDataTenant", balance=Decimal("100.00"), is_active=True)
        # Create successful, processing, and failed tasks
        task_ok = EmailTask(
            id=f"task_ok_{uuid.uuid4().hex[:6]}",
            tenant_id=t_id,
            status="SUCCESS",
            duration_ms=450,
            created_at=now,
            completed_at=now,
            charged_amount=Decimal("0.50"),
            is_charged=True,
        )
        task_proc = EmailTask(
            id=f"task_proc_{uuid.uuid4().hex[:6]}",
            tenant_id=t_id,
            status="PROCESSING",
            created_at=now,
            lease_expires_at=now - timedelta(minutes=5),  # Stale lease!
        )
        task_fail = EmailTask(
            id=f"task_fail_{uuid.uuid4().hex[:6]}",
            tenant_id=t_id,
            status="FAILED",
            created_at=now,
        )
        tx = BillingTransaction(
            tenant_id=t_id,
            type="DEDUCTION",
            amount=Decimal("0.50"),
            balance_before=Decimal("100.00"),
            balance_after=Decimal("99.50"),
            description="Test Deduction",
        )
        db.add(tenant)
        db.add(task_ok)
        db.add(task_proc)
        db.add(task_fail)
        db.add(tx)
        await db.commit()

    # Backup and restore drill timestamp files
    backup_file = tmp_path / ".last_backup_success"
    backup_file.write_text(str(now.timestamp()), encoding="utf-8")
    restore_file = tmp_path / ".last_restore_drill_success"
    restore_file.write_text(str(now.timestamp()), encoding="utf-8")
    redis_backup_file = tmp_path / ".last_redis_backup_success"
    redis_backup_file.write_text(str(now.timestamp()), encoding="utf-8")

    transport = ASGITransport(app=monitor_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_redis = MagicMock()
        mock_redis.llen.return_value = 2
        mock_redis.hgetall.return_value = {"attempts:success": "5", "latency_sum": "2.5", "latency_count": "5"}

        mock_celery = MagicMock()
        mock_celery.control.inspect.return_value.active_queues.return_value = {
            "worker_a": [{"name": settings.CELERY_QUEUE_NAME}]
        }

        with patch("app.monitor.get_redis", return_value=mock_redis), \
             patch("app.monitor.celery_app", mock_celery), \
             patch("app.monitor.Path", return_value=tmp_path):
            res = await client.get("/metrics")
            assert res.status_code == 200
            assert "cargoplus_tasks" in res.text
            assert "cargoplus_revenue_total_yuan" in res.text
            assert "cargoplus_stale_task_leases" in res.text
            assert "cargoplus_redis_backup_age_seconds" in res.text


def test_config_production_all_error_branches():
    # 1. Insecure session secret
    with pytest.raises(RuntimeError):
        Settings(
            ENVIRONMENT="production",
            ADMIN_SECRET_KEY="12345678901234567890123456789012_admin",
            SESSION_SECRET_KEY="cargo-plus-admin-secret-2026",
        ).validate_security_settings()

    # 2. Short secret (<32 chars)
    with pytest.raises(RuntimeError):
        Settings(
            ENVIRONMENT="production",
            ADMIN_SECRET_KEY="short_admin",
            SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
        ).validate_security_settings()

    # 3. Missing LLM key
    with pytest.raises(RuntimeError):
        Settings(
            ENVIRONMENT="production",
            ADMIN_SECRET_KEY="12345678901234567890123456789012_admin",
            SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
            LLM_API_KEY="",
        ).validate_security_settings()

    # 4. Non-Postgres DB in prod
    with pytest.raises(RuntimeError):
        Settings(
            ENVIRONMENT="production",
            ADMIN_SECRET_KEY="12345678901234567890123456789012_admin",
            SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
            LLM_API_KEY="valid_key",
            DATABASE_URL="sqlite+aiosqlite:///prod.db",
        ).validate_security_settings()

    # 5. Non-Redis broker in prod
    with pytest.raises(RuntimeError):
        Settings(
            ENVIRONMENT="production",
            ADMIN_SECRET_KEY="12345678901234567890123456789012_admin",
            SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
            LLM_API_KEY="valid_key",
            DATABASE_URL="postgresql+asyncpg://user:pwd@db:5432/db",
            CELERY_BROKER_URL="amqp://guest:guest@localhost:5672//",
        ).validate_security_settings()
