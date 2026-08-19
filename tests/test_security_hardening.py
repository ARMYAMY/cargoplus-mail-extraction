from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings, settings
from app.core.rate_limit import client_rate_limit_identity
from app.main import protect_public_auth_endpoints


def production_settings(**overrides) -> Settings:
    values = {
        "ENVIRONMENT": "production",
        "ADMIN_SECRET_KEY": "a" * 48,
        "SESSION_SECRET_KEY": "b" * 48,
        "LLM_API_KEY": "llm-secret-value",
        "LLM_BASE_URL": "https://llm.example.com/v1",
        "DATABASE_URL": "postgresql+asyncpg://cargo:secret@postgres/cargo",
        "CELERY_BROKER_URL": "redis://:secret@redis:6379/0",
        "CELERY_RESULT_BACKEND": "redis://:secret@redis:6379/1",
        "TASK_QUEUE_MODE": "celery",
        "CORS_ALLOWED_ORIGINS": "https://cargo.example.com",
        "ALLOWED_HOSTS": "cargo.example.com,api,127.0.0.1",
        "AUTH_RATE_LIMIT_ENABLED": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_request(path: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("172.20.0.4", 12345),
            "scheme": "http",
            "server": ("api", 8000),
        }
    )


def test_production_security_validation_accepts_hardened_configuration():
    production_settings().validate_security_settings()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("CORS_ALLOWED_ORIGINS", "http://cargo.example.com", "HTTPS origins"),
        ("ALLOWED_HOSTS", "*", "must be explicit"),
        ("LLM_BASE_URL", "http://llm.internal/v1", "must use HTTPS"),
        ("TASK_QUEUE_MODE", "local", "must be 'celery'"),
        ("AUTH_RATE_LIMIT_ENABLED", False, "must be enabled"),
    ],
)
def test_production_security_validation_rejects_unsafe_configuration(field, value, message):
    candidate = production_settings(**{field: value})
    with pytest.raises(RuntimeError, match=message):
        candidate.validate_security_settings()


def test_proxy_client_identity_uses_only_valid_forwarded_ip(monkeypatch):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    forwarded = client_rate_limit_identity(make_request("/api/v1/auth/login", "198.51.100.8"))
    malformed = client_rate_limit_identity(make_request("/api/v1/auth/login", "not-an-ip"))
    direct = client_rate_limit_identity(make_request("/api/v1/auth/login"))
    assert forwarded != direct
    assert malformed == direct


@pytest.mark.asyncio
async def test_auth_rate_limit_returns_429_before_login_handler(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr("app.main.consume_rate_limit", AsyncMock(return_value=(False, 42)))
    call_next = AsyncMock(return_value=Response(status_code=204))

    response = await protect_public_auth_endpoints(
        make_request("/api/v1/auth/admin/login"),
        call_next,
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    call_next.assert_not_awaited()
