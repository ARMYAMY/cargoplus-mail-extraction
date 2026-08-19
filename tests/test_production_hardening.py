from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.celery_tasks import recover_stale_tasks
from app.config import Settings
from app.core.skill_runner import NonRetryableLLMError, SkillRunner


def write_secret(path: Path, value: str) -> str:
    path.write_text(value, encoding="utf-8")
    return str(path)


def test_settings_load_docker_secret_files(tmp_path: Path):
    database_url = "postgresql+asyncpg://cargo:password@postgres:5432/cargo"
    settings = Settings(
        _env_file=None,
        DATABASE_URL_FILE=write_secret(tmp_path / "database", database_url),
        CELERY_BROKER_URL_FILE=write_secret(tmp_path / "broker", "redis://redis:6379/0"),
        CELERY_RESULT_BACKEND_FILE=write_secret(tmp_path / "backend", "redis://redis:6379/1"),
        LLM_API_KEY_FILE=write_secret(tmp_path / "llm", "llm-secret"),
        ADMIN_SECRET_KEY_FILE=write_secret(tmp_path / "admin", "a" * 48),
        SESSION_SECRET_KEY_FILE=write_secret(tmp_path / "session", "b" * 48),
    )
    assert settings.DATABASE_URL == database_url
    assert settings.CELERY_BROKER_URL == "redis://redis:6379/0"
    assert settings.LLM_API_KEY == "llm-secret"
    assert settings.ADMIN_SECRET_KEY == "a" * 48


def test_production_security_rejects_shared_secrets():
    settings = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://cargo:password@postgres:5432/cargo",
        CELERY_BROKER_URL="redis://redis:6379/0",
        LLM_API_KEY="real-key",
        ADMIN_SECRET_KEY="x" * 40,
        SESSION_SECRET_KEY="x" * 40,
        CORS_ALLOWED_ORIGINS="https://cargo.example.test",
    )
    with pytest.raises(RuntimeError, match="must be different"):
        settings.validate_security_settings()


def test_production_security_rejects_non_redis_result_backend():
    settings = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://cargo:password@postgres:5432/cargo",
        CELERY_BROKER_URL="rediss://redis:6379/0",
        CELERY_RESULT_BACKEND="rpc://",
        LLM_API_KEY="real-key",
        ADMIN_SECRET_KEY="a" * 40,
        SESSION_SECRET_KEY="b" * 40,
        CORS_ALLOWED_ORIGINS="https://cargo.example.test",
    )
    with pytest.raises(RuntimeError, match="RESULT_BACKEND"):
        settings.validate_security_settings()


def test_duplicate_beat_invocation_is_skipped():
    redis_client = MagicMock()
    redis_client.set.return_value = False
    with patch("app.celery_tasks._redis", return_value=redis_client), patch(
        "app.celery_tasks._prepare_recovery_batch"
    ) as prepare:
        assert recover_stale_tasks() == 0
        prepare.assert_not_called()


@pytest.mark.asyncio
async def test_non_retryable_llm_4xx_is_not_retried(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "extract.md").write_text("{{mail_body}}", encoding="utf-8")
    (prompt_dir / "validate.md").write_text("", encoding="utf-8")
    runner = SkillRunner(skill_path=tmp_path)

    response = MagicMock(status_code=400, headers={})
    client = AsyncMock()
    client.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    context.__aexit__.return_value = False

    with patch("app.core.skill_runner.httpx.AsyncClient", return_value=context), patch(
        "app.core.skill_runner.record_llm_attempt", new_callable=AsyncMock
    ):
        with pytest.raises(NonRetryableLLMError):
            await runner.call_llm("invalid request")
    assert client.post.await_count == 1
