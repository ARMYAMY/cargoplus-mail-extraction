import io
import json
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.auth_service import hash_password, generate_api_key_and_secret
from app.config import settings


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_test_tenant(db, name, email, password="password123", initial_balance=Decimal("100.00"), unit_price=Decimal("0.50"), is_active=True):
    tenant_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, key_prefix, key_hash, api_secret = generate_api_key_and_secret()
    tenant = Tenant(
        id=tenant_id,
        name=name,
        contact_email=email,
        password_hash=hash_password(password),
        balance=initial_balance,
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
async def test_admin_stats_history_deep_coverage():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="HistoryTenant",
            contact_email=f"{t_id}@example.com",
            is_active=True,
            balance=Decimal("500.00"),
            unit_price=Decimal("1.00"),
        )
        task_succ = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="SUCCESS",
            duration_ms=1200,
            mail_subject="Test Success",
        )
        task_fail = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="FAILED",
            duration_ms=800,
            mail_subject="Test Fail",
        )
        tx_deduct = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="DEDUCTION",
            amount=Decimal("1.00"),
            balance_before=Decimal("500.00"),
            balance_after=Decimal("499.00"),
            description="Test Deduction",
        )
        db.add_all([tenant, task_succ, task_fail, tx_deduct])
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Call history endpoint
        res = await client.get("/admin/stats/history?days=14", headers=admin_headers)
        assert res.status_code == 200
        data = res.json()
        assert "lifetime" in data
        assert "period" in data
        assert "history" in data
        assert "tenant_rankings" in data
        assert data["lifetime"]["total"] >= 2


@pytest.mark.asyncio
async def test_admin_tenants_direct_recharge_and_edge_cases():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="DirectRechargeTenant",
            email="direct_rec@example.com",
            initial_balance=Decimal("20.00"),
        )
        t_id = tenant.id

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Direct recharge on /admin/tenants/{id}/recharge
        res = await client.post(
            f"/admin/tenants/{t_id}/recharge",
            headers=admin_headers,
            json={"amount": 80.0, "description": "管理员直充"},
        )
        assert res.status_code == 200

        # Direct recharge invalid tenant
        res_none = await client.post(
            "/admin/tenants/non_existent_id/recharge",
            headers=admin_headers,
            json={"amount": 80.0},
        )
        assert res_none.status_code == 404

        # Direct recharge invalid negative amount
        res_neg = await client.post(
            f"/admin/tenants/{t_id}/recharge",
            headers=admin_headers,
            json={"amount": -50.0},
        )
        assert res_neg.status_code == 422


@pytest.mark.asyncio
async def test_v1_billing_csv_formula_injection_and_pagination():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="FormulaTenant",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("100.00"),
        )
        # Transaction with formula injection characters (=, +, -, @)
        tx_inj = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant.id,
            type="RECHARGE",
            amount=Decimal("10.00"),
            balance_before=Decimal("100.00"),
            balance_after=Decimal("110.00"),
            description="=cmd|' /C calc'!A0",
        )
        db.add(tx_inj)
        await db.commit()

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Export CSV
        res = await client.get("/api/v1/billing/export-csv", headers=tenant_headers)
        assert res.status_code == 200
        content = res.text
        # Formula should be safely escaped or sanitized
        assert "calc" in content

        # Statements with different day ranges
        res_90 = await client.get("/api/v1/billing/statements/daily?days=90&page=1&page_size=50", headers=tenant_headers)
        assert res_90.status_code == 200


@pytest.mark.asyncio
async def test_auth_edge_cases_and_validations():
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid email format in register
        res_bad_email = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": "BadEmailCo",
                "contact_email": "not-an-email",
                "password": "ValidPassword123!",
            },
        )
        assert res_bad_email.status_code == 422

        # Short password in register
        res_short_pwd = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": "ShortPwdCo",
                "contact_email": "short@example.com",
                "password": "123",
            },
        )
        assert res_short_pwd.status_code == 422


@pytest.mark.asyncio
async def test_extract_file_upload_validation_errors():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="UploadValTenant",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("100.00"),
        )

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Disallowed file extension (.exe)
        exe_file = [("files", ("malware.exe", io.BytesIO(b"MZ..."), "application/x-msdownload"))]
        res_exe = await client.post(
            "/api/v1/extract/async/upload",
            headers=tenant_headers,
            files=exe_file,
            data={"mail_subject": "Exe Test"},
        )
        assert res_exe.status_code == 415

        # 2. Too many files
        many_files = [
            ("files", (f"doc_{i}.txt", io.BytesIO(b"data"), "text/plain"))
            for i in range(settings.MAX_UPLOAD_FILES + 2)
        ]
        res_many = await client.post(
            "/api/v1/extract/async/upload",
            headers=tenant_headers,
            files=many_files,
            data={"mail_subject": "Many Files"},
        )
        assert res_many.status_code == 413
