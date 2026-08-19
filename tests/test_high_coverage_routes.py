import io
import json
import uuid
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
from app.services.auth_service import hash_password, generate_api_key_and_secret, create_access_token
from app.config import settings


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_test_tenant(db, name, email, password="password123", balance=Decimal("100.00"), unit_price=Decimal("0.50"), is_active=True):
    tenant_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, key_prefix, key_hash, api_secret = generate_api_key_and_secret()
    tenant = Tenant(
        id=tenant_id,
        name=name,
        contact_email=email,
        password_hash=hash_password(password),
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
async def test_v1_extract_all_branches():
    transport = ASGITransport(app=app)
    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(db, "ExtractFullCo", "extfull@example.com", balance=Decimal("200.00"))
        t_id = tenant.id

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. extract_async_json with callback_url
        with patch("app.api.v1.extract.is_safe_webhook_url", return_value=True):
            res_async = await client.post(
                "/api/v1/extract/async",
                headers=tenant_headers,
                json={
                    "mail_subject": "Booking MSC",
                    "mail_body": "POL: SHANGHAI POD: HAMBURG",
                    "attachments": [
                        {"filename": "test.txt", "content_type": "text/plain", "text": "POL: SHANGHAI"}
                    ],
                    "callback_url": "https://api.example.com/webhook",
                },
            )
            assert res_async.status_code == 200
            assert "task_id" in res_async.json()

        # 2. extract_async_upload with multiple files (.txt, .eml dummy)
        txt_bytes = b"VESSEL: MSC OSCAR\nPOL: YANTIAN"
        files = [
            ("files", ("booking.txt", io.BytesIO(txt_bytes), "text/plain")),
            ("files", ("packing.csv", io.BytesIO(b"Container,Type\nMSCU123,40HQ"), "text/csv")),
        ]
        res_upload = await client.post(
            "/api/v1/extract/async/upload",
            headers=tenant_headers,
            files=files,
            data={"mail_subject": "Upload Multi Test"},
        )
        assert res_upload.status_code == 200

        # 3. extract_sync failure branch (raises HTTP 500 when task status is FAILED)
        with patch("app.core.skill_runner.SkillRunner.extract_draft_json", side_effect=RuntimeError("LLM API exploded")):
            res_sync_fail = await client.post(
                "/api/v1/extract/sync",
                headers=tenant_headers,
                json={"mail_subject": "Fail Test", "mail_body": "Fail Body"},
            )
            assert res_sync_fail.status_code == 500
            assert "Extraction failed" in res_sync_fail.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_v1_auth_all_branches():
    transport = ASGITransport(app=app)
    email = f"auth_branch_{uuid.uuid4().hex[:6]}@example.com"
    pwd = "ValidPassword123!"

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register
        res_reg = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": "AuthBranchCo",
                "contact_email": email,
                "password": pwd,
            },
        )
        assert res_reg.status_code == 200
        t_id = res_reg.json()["data"]["tenant_id"]
        raw_key = res_reg.json()["data"]["api_key"]

        # 2. Duplicate registration -> 400
        res_dup = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": "AuthBranchCo",
                "contact_email": email,
                "password": pwd,
            },
        )
        assert res_dup.status_code == 400

        # 3. Login with API key when inactive -> 403
        res_key_inact = await client.post(
            "/api/v1/auth/login",
            json={"account": raw_key},
        )
        assert res_key_inact.status_code == 403

        # 4. Login with email when inactive -> 403
        res_mail_inact = await client.post(
            "/api/v1/auth/login",
            json={"account": email, "password": pwd},
        )
        assert res_mail_inact.status_code == 403

        # 5. Admin activates tenant
        await client.put(
            f"/admin/tenants/{t_id}/status?is_active=true",
            headers={"X-Admin-Secret": settings.ADMIN_SECRET_KEY},
        )

        # 6. Login with API Key when active -> 200
        res_key_act = await client.post(
            "/api/v1/auth/login",
            json={"account": raw_key},
        )
        assert res_key_act.status_code == 200

        # 7. Login with email and missing password -> 400
        res_no_pwd = await client.post(
            "/api/v1/auth/login",
            json={"account": email},
        )
        assert res_no_pwd.status_code == 400

        # 8. Login with email and wrong password -> 401
        res_wrong_pwd = await client.post(
            "/api/v1/auth/login",
            json={"account": email, "password": "WrongPassword!"},
        )
        assert res_wrong_pwd.status_code == 401

        # 9. Login with invalid API Key -> 401
        res_bad_key = await client.post(
            "/api/v1/auth/login",
            json={"account": "cg_invalid_key_12345"},
        )
        assert res_bad_key.status_code == 401

        # 10. Admin login success & failure
        res_adm_ok = await client.post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": settings.ADMIN_SECRET_KEY},
        )
        assert res_adm_ok.status_code == 200

        res_adm_bad = await client.post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": "bad-admin-secret"},
        )
        assert res_adm_bad.status_code == 401


@pytest.mark.asyncio
async def test_v1_billing_all_branches():
    transport = ASGITransport(app=app)
    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(db, "BillBranchCo", "billbranch@example.com", balance=Decimal("300.00"))
        t_id = tenant.id

        # Insert DEDUCTION and RECHARGE transactions
        tx1 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="DEDUCTION",
            amount=Decimal("0.50"),
            balance_before=Decimal("300.00"),
            balance_after=Decimal("299.50"),
            description="API deduction test",
        )
        tx2 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="RECHARGE",
            amount=Decimal("100.00"),
            balance_before=Decimal("299.50"),
            balance_after=Decimal("399.50"),
            description="Recharge test",
        )
        db.add_all([tx1, tx2])
        await db.commit()

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. get_billing_transactions with limit param
        res_limit = await client.get("/api/v1/billing/transactions?limit=5", headers=tenant_headers)
        assert res_limit.status_code == 200
        assert len(res_limit.json()["items"]) >= 2

        # 2. get_daily_statements with aggregation
        res_stmt = await client.get("/api/v1/billing/statements/daily?days=7&page=1&page_size=10", headers=tenant_headers)
        assert res_stmt.status_code == 200
        assert res_stmt.json()["total"] >= 1
        day_items = res_stmt.json()["items"]
        assert any(item["deduction_count"] >= 1 for item in day_items)
        assert any(item["recharge_count"] >= 1 for item in day_items)

        # 3. export_billing_csv with type filter
        res_csv = await client.get("/api/v1/billing/export-csv?type=DEDUCTION", headers=tenant_headers)
        assert res_csv.status_code == 200
        assert "API 扣费" in res_csv.text
