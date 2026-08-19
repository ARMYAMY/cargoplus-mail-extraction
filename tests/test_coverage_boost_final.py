import asyncio
from decimal import Decimal
import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.main import app, seed_initial_demo_tenant, lifespan
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.api.deps import get_current_tenant_and_key, verify_admin_access
from app.services.auth_service import (
    create_access_token,
    generate_api_key_and_secret,
    hash_password,
)
from app.core.parser.ocr_engine import get_ocr_engine, extract_ocr_from_image, extract_ocr_from_bytes
from app.core.parser.pdf_parser import parse_pdf
from app.config import settings


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_test_tenant(db, name, email, balance=Decimal("100.00"), unit_price=Decimal("0.50"), is_active=True):
    tenant_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, key_prefix, key_hash, api_secret = generate_api_key_and_secret()
    tenant = Tenant(
        id=tenant_id,
        name=name,
        contact_email=email,
        password_hash=hash_password("password123"),
        balance=balance,
        unit_price=unit_price,
        is_active=is_active,
    )
    api_key = ApiKey(
        id=f"key_{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        api_secret=api_secret,
        name="默认密钥",
        is_active=True,
    )
    db.add(tenant)
    db.add(api_key)
    await db.commit()
    return tenant, raw_key


@pytest.mark.asyncio
async def test_deps_all_branches():
    transport = ASGITransport(app=app)
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(db, "DepsTenant", "deps@example.com")
        t_id = tenant.id

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Admin access with X-Tenant-ID header
        res_adm_target = await client.get(
            "/api/v1/tenants/me",
            headers={"X-Admin-Secret": settings.ADMIN_SECRET_KEY, "X-Tenant-ID": t_id},
        )
        assert res_adm_target.status_code == 200

        # 2. Admin access via Bearer admin session token
        admin_token = create_access_token("admin", role="admin")
        res_adm_token = await client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {admin_token}", "X-Tenant-ID": t_id},
        )
        assert res_adm_token.status_code == 200

        # 3. Tenant access via X-API-Key header
        res_x_key = await client.get(
            "/api/v1/tenants/me",
            headers={"X-API-Key": raw_key},
        )
        assert res_x_key.status_code == 200

        # 4. Tenant access via signed session token
        tenant_token = create_access_token(t_id, role="tenant")
        res_t_token = await client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {tenant_token}"},
        )
        assert res_t_token.status_code == 200

        # 5. Invalid admin access header -> 403
        res_bad_adm = await client.get(
            "/admin/tenants",
            headers={"X-Admin-Secret": "invalid_secret_key"},
        )
        assert res_bad_adm.status_code == 403


@pytest.mark.asyncio
async def test_main_lifespan_and_seed():
    # 1. Seed demo tenant
    await seed_initial_demo_tenant()

    # 2. Run Lifespan startup and shutdown
    async with lifespan(app):
        assert app is not None


def test_ocr_engine_mocked_results(tmp_path):
    # Mock RapidOCR engine
    mock_ocr = MagicMock()
    mock_ocr.return_value = ([
        [None, "CARGO CONTAINER OCR LINE 1"],
        [None, "CARGO CONTAINER OCR LINE 2"],
    ], [0.1, 0.2])

    with patch("app.core.parser.ocr_engine.get_ocr_engine", return_value=mock_ocr):
        dummy_img = tmp_path / "ocr_test.jpg"
        dummy_img.write_bytes(b"\xff\xd8\xff\xe0 dummy jpeg")

        text_img = extract_ocr_from_image(dummy_img)
        assert "CARGO CONTAINER OCR LINE 1" in text_img

        text_bytes = extract_ocr_from_bytes(b"dummy bytes")
        assert "CARGO CONTAINER OCR LINE 2" in text_bytes


def test_pdf_parser_scanned_ocr_fallback(tmp_path):
    dummy_pdf = tmp_path / "scanned.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy scanned")

    with patch("app.core.parser.pdf_parser.PdfReader") as mock_reader, \
         patch("app.core.parser.pdf_parser.extract_ocr_from_bytes", return_value="SCANNED OCR TEXT"):
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""  # Empty text (scanned PDF)
        mock_img = MagicMock()
        mock_img.data = b"image_data"
        mock_page.images = [mock_img]
        mock_reader.return_value.pages = [mock_page]

        text, tables, ocr_text = parse_pdf(dummy_pdf)
        assert "SCANNED OCR TEXT" in ocr_text


@pytest.mark.asyncio
async def test_admin_unit_price_update_api():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        tenant, _ = await create_test_tenant(db, "PriceTenant", "price@example.com")
        t_id = tenant.id

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # PUT /admin/tenants/{id}/unit-price
        res = await client.put(
            f"/admin/tenants/{t_id}/unit-price",
            headers=admin_headers,
            json={"unit_price": 2.50},
        )
        assert res.status_code == 200

        # Non-existent tenant
        res_none = await client.put(
            "/admin/tenants/non_existent_id/unit-price",
            headers=admin_headers,
            json={"unit_price": 2.50},
        )
        assert res_none.status_code == 404
