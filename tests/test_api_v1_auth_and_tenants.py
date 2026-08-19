import uuid
from decimal import Decimal
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.services.auth_service import hash_password, generate_api_key_and_secret, hash_api_key
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
async def test_auth_registration_and_login_lifecycle():
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register a new tenant
        company_name = f"RegCo_{uuid.uuid4().hex[:6]}"
        email = f"{company_name}@example.com"
        pwd = "SecurePassword123!"

        res = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": company_name,
                "contact_email": email,
                "contact_phone": "13812345678",
                "password": pwd,
            },
        )
        assert res.status_code == 200
        reg_data = res.json()["data"]
        tenant_id = reg_data["tenant_id"]
        raw_key = reg_data["api_key"]
        assert raw_key.startswith("cg_")

        # Duplicate company registration rejection (same company, different email)
        res_dup_company = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": company_name,
                "contact_email": f"other_{uuid.uuid4().hex[:6]}@example.com",
                "password": pwd,
            },
        )
        assert res_dup_company.status_code == 400
        assert res_dup_company.json()["detail"]["code"] == 40002

        # Duplicate email registration rejection (different company, same email)
        res_dup_email = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": f"OtherCo_{uuid.uuid4().hex[:6]}",
                "contact_email": email,
                "password": pwd,
            },
        )
        assert res_dup_email.status_code == 400
        assert res_dup_email.json()["detail"]["code"] == 40001

        # 2. Inactive login attempt (default registration is pending audit)
        res_inactive_login = await client.post(
            "/api/v1/auth/login",
            json={"account": email, "password": pwd},
        )
        # Should be 403 Forbidden because tenant is pending audit
        assert res_inactive_login.status_code == 403

        # 3. Admin audits and approves tenant
        admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
        res_audit = await client.put(
            f"/admin/tenants/{tenant_id}/status?is_active=true",
            headers=admin_headers,
        )
        assert res_audit.status_code == 200

        # 4. Successful login by email & password
        res_login_ok = await client.post(
            "/api/v1/auth/login",
            json={"account": email, "password": pwd},
        )
        assert res_login_ok.status_code == 200
        login_token = res_login_ok.json()["data"]["token"]
        assert login_token is not None

        # Wrong password login
        res_bad_pwd = await client.post(
            "/api/v1/auth/login",
            json={"account": email, "password": "WrongPassword!"},
        )
        assert res_bad_pwd.status_code == 401

        # Non-existent account login
        res_no_acc = await client.post(
            "/api/v1/auth/login",
            json={"account": "non_existent@example.com", "password": pwd},
        )
        assert res_no_acc.status_code == 401

        # 5. Login by API Key
        res_key_login = await client.post(
            "/api/v1/auth/login",
            json={"account": raw_key},
        )
        assert res_key_login.status_code == 200

        # 6. Admin Login
        res_admin_login = await client.post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": settings.ADMIN_SECRET_KEY},
        )
        assert res_admin_login.status_code == 200
        assert "admin_token" in res_admin_login.json()["data"]

        # Admin login failure
        res_admin_bad = await client.post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": "wrong-secret"},
        )
        assert res_admin_bad.status_code == 401


@pytest.mark.asyncio
async def test_tenant_me_and_keys_endpoints():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="TenantMeCo",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("100.00"),
            unit_price=Decimal("0.50"),
        )

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET /api/v1/tenants/me
        res = await client.get("/api/v1/tenants/me", headers=tenant_headers)
        assert res.status_code == 200
        me_data = res.json()
        assert me_data["id"] == tenant.id
        assert Decimal(str(me_data["balance"])) == Decimal("100.00")

        # 2. GET /api/v1/tenants/me/keys
        res = await client.get("/api/v1/tenants/me/keys", headers=tenant_headers)
        assert res.status_code == 200
        keys = res.json()
        assert len(keys) >= 1

        # 3. POST /api/v1/tenants/me/keys
        res = await client.post(
            "/api/v1/tenants/me/keys?key_name=Secondary_ERP_Key",
            headers=tenant_headers,
        )
        assert res.status_code == 200
        new_k = res.json()
        assert "raw_api_key" in new_k
        assert new_k["name"] == "Secondary_ERP_Key"

        # 4. Verify auth failure without token
        res_no_auth = await client.get("/api/v1/tenants/me")
        assert res_no_auth.status_code == 401

        # 5. Verify invalid token
        res_invalid = await client.get("/api/v1/tenants/me", headers={"Authorization": "Bearer invalid_key"})
        assert res_invalid.status_code == 401
