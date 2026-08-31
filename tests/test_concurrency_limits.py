from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.limits import MAX_TENANT_CONCURRENCY, MIN_TENANT_CONCURRENCY
from app.schemas.tenant import TenantCreate, TenantUpdate


@pytest.mark.parametrize("value", [0, MAX_TENANT_CONCURRENCY + 1, 1.5, True, "10"])
def test_tenant_request_models_reject_invalid_concurrency(value):
    with pytest.raises(ValidationError):
        TenantCreate(name="并发边界测试", max_concurrency=value)
    with pytest.raises(ValidationError):
        TenantUpdate(max_concurrency=value)


def test_tenant_request_models_accept_exact_concurrency_boundaries():
    assert TenantCreate(
        name="最小并发测试",
        max_concurrency=MIN_TENANT_CONCURRENCY,
    ).max_concurrency == MIN_TENANT_CONCURRENCY
    assert TenantUpdate(
        max_concurrency=MAX_TENANT_CONCURRENCY,
    ).max_concurrency == MAX_TENANT_CONCURRENCY


@pytest.mark.parametrize(
    "overrides",
    [
        {"DEFAULT_TENANT_CONCURRENCY": 0},
        {"DEFAULT_TENANT_CONCURRENCY": 31},
        {"WORKER_CONCURRENCY": 0},
        {"WORKER_CONCURRENCY": 101},
    ],
)
def test_settings_reject_invalid_concurrency(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_frontend_concurrency_limits_match_backend_constants():
    project_root = Path(__file__).resolve().parents[1]
    html = (project_root / "app/static/index.html").read_text(encoding="utf-8")
    javascript = (project_root / "app/static/js/app.js").read_text(encoding="utf-8")

    assert 'id="ct-concurrency"' in html
    assert 'min="1" max="30" step="1"' in html
    assert f"const MAX_TENANT_CONCURRENCY = {MAX_TENANT_CONCURRENCY};" in javascript
    assert "adminFetch('/admin/tasks/statuses'" in javascript
    assert "/admin/tasks?page=2&page_size=100" not in javascript
    assert "120000" not in html
    assert "legacyBenchmarkImplementationNotUsed" not in html
    assert '/static/js/app.js?v=20260831v64' in html
