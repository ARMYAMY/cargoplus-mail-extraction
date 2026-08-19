import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.config import settings, Settings
from app.database import init_db, get_db, AsyncSessionLocal
from app.schemas.cargo_v3 import CargoV3Output, ContainerInfoItem
from app.schemas.tenant import TenantCreate, TenantUpdate, TenantResponse
from app.schemas.task import ExtractSyncRequest, ExtractAsyncRequest, AttachmentInput
from app.schemas.billing import BillingSummaryResponse, BillingTransactionResponse


@pytest.mark.asyncio
async def test_main_routes_and_security_headers():
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health check
        res_health = await client.get("/health")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "healthy"

        # 2. Security headers check
        assert res_health.headers.get("X-Content-Type-Options") == "nosniff"
        assert res_health.headers.get("X-Frame-Options") == "DENY"

        # 3. Static Pages HTML routes
        res_login = await client.get("/login")
        assert res_login.status_code == 200
        assert "CargoPlus" in res_login.text or "登录" in res_login.text

        res_portal = await client.get("/portal")
        assert res_portal.status_code == 200

        res_reg = await client.get("/register")
        assert res_reg.status_code == 200

        res_root = await client.get("/")
        assert res_root.status_code == 200


def test_settings_and_config():
    cfg = Settings(
        ENVIRONMENT="development",
        DEBUG=True,
        DATABASE_URL="sqlite+aiosqlite:///./test_cfg.db",
    )
    assert cfg.ENVIRONMENT == "development"
    assert cfg.DEBUG is True
    assert cfg.cors_allowed_origins is not None
    assert cfg.session_secret is not None
    cfg.validate_security_settings()


def test_pydantic_schemas():
    # 1. Cargo schemas
    container = ContainerInfoItem(ContainerNo="MSCU1234567", ContType="40HQ")
    cargo_resp = CargoV3Output(BookingNo="BK123", POL="Yantian", POD="Hamburg")
    assert cargo_resp.BookingNo == "BK123"

    # 2. Tenant schemas
    t_create = TenantCreate(name="TestSchemaCo", unit_price=0.8)
    assert t_create.name == "TestSchemaCo"
    t_update = TenantUpdate(name="TestSchemaCo2", is_active=False)
    assert t_update.is_active is False

    # 3. Task schemas
    task_req = ExtractSyncRequest(mail_subject="Schema task subject", mail_body="Valid body content")
    assert task_req.mail_subject == "Schema task subject"
