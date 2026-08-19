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
from fastapi import HTTPException, Response
from httpx import ASGITransport, AsyncClient

from app.config import settings, Settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.main import app as main_app
from app.monitor import app as monitor_app, _queue_and_worker_snapshot
from app.core.observability import record_llm_attempt, api_metrics_payload
from app.core.redis_client import _sentinel_nodes, get_redis, get_async_redis


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_monitor_app_endpoints(tmp_path):
    transport = ASGITransport(app=monitor_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. /health/live
        res_live = await client.get("/health/live")
        assert res_live.status_code == 200
        assert res_live.json() == {"status": "alive"}

        # 2. /metrics
        mock_redis = MagicMock()
        mock_redis.llen.return_value = 5
        mock_redis.hgetall.return_value = {
            "attempts:success": "10",
            "latency_sum": "5.5",
            "latency_count": "10",
        }

        mock_celery = MagicMock()
        mock_celery.control.inspect.return_value.active_queues.return_value = {
            "worker1@host": [{"name": settings.CELERY_QUEUE_NAME}]
        }

        with patch("app.monitor.get_redis", return_value=mock_redis), \
             patch("app.monitor.celery_app", mock_celery):
            res_metrics = await client.get("/metrics")
            assert res_metrics.status_code == 200
            assert "cargoplus_tasks" in res_metrics.text
            assert "cargoplus_queue_depth" in res_metrics.text
            assert "cargoplus_llm_attempts_total" in res_metrics.text
            assert "cargoplus_celery_beat_tick_age_seconds" in res_metrics.text


@pytest.mark.asyncio
async def test_observability_and_main_metrics():
    # 1. record_llm_attempt with metrics enabled
    mock_async_redis = MagicMock()
    mock_pipe = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_async_redis.pipeline.return_value = mock_pipe
    mock_async_redis.aclose = AsyncMock()

    with patch.object(settings, "METRICS_ENABLED", True), \
         patch("app.core.observability.get_async_redis", return_value=mock_async_redis):
        await record_llm_attempt("success", 0.45)
        mock_pipe.hincrby.assert_any_call("cargoplus:metrics:llm", "attempts:success", 1)
        mock_pipe.hincrbyfloat.assert_called_once_with("cargoplus:metrics:llm", "latency_sum", 0.45)

    # 2. record_llm_attempt with metrics disabled
    with patch.object(settings, "METRICS_ENABLED", False):
        await record_llm_attempt("failed", 1.2)

    # 3. api_metrics_payload
    payload, content_type = api_metrics_payload()
    assert isinstance(payload, bytes)
    assert content_type.startswith("text/plain")

    # 4. main_app /metrics endpoint and middleware
    transport = ASGITransport(app=main_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch.object(settings, "METRICS_ENABLED", True):
            res = await client.get("/metrics")
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/plain")

        with patch.object(settings, "METRICS_ENABLED", False):
            res_off = await client.get("/metrics")
            assert res_off.status_code == 404


def test_redis_client_sentinel_and_standalone():
    # 1. _sentinel_nodes
    with patch.object(settings, "REDIS_SENTINEL_URLS", "sentinel1:26379, sentinel2:26380"):
        nodes = _sentinel_nodes()
        assert len(nodes) == 2
        assert nodes[0] == ("sentinel1", 26379)
        assert nodes[1] == ("sentinel2", 26380)

    with patch.object(settings, "REDIS_SENTINEL_URLS", "://bad-node"):
        with pytest.raises(ValueError):
            _sentinel_nodes()

    # 2. get_redis and get_async_redis standalone
    with patch.object(settings, "REDIS_SENTINEL_URLS", ""), \
         patch("app.core.redis_client.Redis.from_url") as mock_from_url:
        get_redis(decode_responses=True)
        mock_from_url.assert_called_once()

    with patch.object(settings, "REDIS_SENTINEL_URLS", ""), \
         patch("app.core.redis_client.AsyncRedis.from_url") as mock_afrom_url:
        get_async_redis(decode_responses=True)
        mock_afrom_url.assert_called_once()

    # 3. get_redis and get_async_redis sentinel
    with patch.object(settings, "REDIS_SENTINEL_URLS", "sentinel1:26379"), \
         patch("app.core.redis_client.Sentinel") as mock_sentinel:
        get_redis(decode_responses=True)
        mock_sentinel.return_value.master_for.assert_called_once()

    with patch.object(settings, "REDIS_SENTINEL_URLS", "sentinel1:26379"), \
         patch("app.core.redis_client.AsyncSentinel") as mock_asentinel:
        get_async_redis(decode_responses=True)
        mock_asentinel.return_value.master_for.assert_called_once()


def test_config_secret_files_and_production_validation(tmp_path):
    # 1. _load_secret_files valid
    secret_f = tmp_path / "secret.txt"
    secret_f.write_text("my-super-secret-key-value", encoding="utf-8")

    cfg = Settings(ADMIN_SECRET_KEY_FILE=str(secret_f))
    assert cfg.ADMIN_SECRET_KEY == "my-super-secret-key-value"

    # 2. _load_secret_files oversized
    big_f = tmp_path / "big.txt"
    big_f.write_text("A" * 70000, encoding="utf-8")
    with pytest.raises(ValueError):
        Settings(ADMIN_SECRET_KEY_FILE=str(big_f))

    # 3. _load_secret_files empty
    empty_f = tmp_path / "empty.txt"
    empty_f.write_text("   ", encoding="utf-8")
    with pytest.raises(ValueError):
        Settings(ADMIN_SECRET_KEY_FILE=str(empty_f))

    # 4. _load_secret_files unreadable
    with pytest.raises(ValueError):
        Settings(ADMIN_SECRET_KEY_FILE=str(tmp_path / "non_existent.txt"))

    # 5. validate_security_settings in production mode
    prod_cfg = Settings(
        ENVIRONMENT="production",
        ADMIN_SECRET_KEY="12345678901234567890123456789012_admin",
        SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
        LLM_API_KEY="valid-llm-key",
        DATABASE_URL="postgresql+asyncpg://user:pwd@db:5432/db",
        CELERY_BROKER_URL="redis://redis:6379/0",
        CORS_ALLOWED_ORIGINS="https://app.example.com",
        ALLOWED_HOSTS="app.example.com,api,127.0.0.1",
        AUTH_RATE_LIMIT_ENABLED=True,
        TASK_QUEUE_MODE="celery",
    )
    prod_cfg.validate_security_settings()

    # Insecure admin secret in prod
    prod_bad_admin = Settings(
        ENVIRONMENT="production",
        ADMIN_SECRET_KEY="change-me",
        SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
    )
    with pytest.raises(RuntimeError):
        prod_bad_admin.validate_security_settings()

    # Identical secrets in prod
    prod_same = Settings(
        ENVIRONMENT="production",
        ADMIN_SECRET_KEY="12345678901234567890123456789012_same",
        SESSION_SECRET_KEY="12345678901234567890123456789012_same",
    )
    with pytest.raises(RuntimeError):
        prod_same.validate_security_settings()

    # Wildcard CORS in prod
    prod_cors = Settings(
        ENVIRONMENT="production",
        ADMIN_SECRET_KEY="12345678901234567890123456789012_admin",
        SESSION_SECRET_KEY="12345678901234567890123456789012_sess",
        LLM_API_KEY="valid-llm-key",
        DATABASE_URL="postgresql+asyncpg://user:pwd@db:5432/db",
        CELERY_BROKER_URL="redis://redis:6379/0",
        CORS_ALLOWED_ORIGINS="*",
    )
    with pytest.raises(RuntimeError):
        prod_cors.validate_security_settings()

    # Invalid queue mode
    prod_queue = Settings(TASK_QUEUE_MODE="invalid_queue")
    with pytest.raises(RuntimeError):
        prod_queue.validate_security_settings()
