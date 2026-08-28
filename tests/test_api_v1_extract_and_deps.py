import io
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.services.auth_service import hash_password, generate_api_key_and_secret
from app.config import settings


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


async def create_test_tenant(db, name, email, password="password123", initial_balance=Decimal("100.00"), unit_price=Decimal("1.00"), is_active=True):
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
async def test_extract_sync_and_async_endpoints():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="ExtractTenantTest",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("50.00"),
            unit_price=Decimal("1.00"),
        )

    tenant_headers = {
        "Authorization": f"Bearer {raw_key}",
        "X-Tenant-ID": tenant.id,
    }

    mock_llm_result = {
        "ShipperName": "SHANGHAI CARGO CO",
        "POLName": "SHANGHAI",
        "PODName": "ROTTERDAM",
        "ContainerInfo": [
            {"ContainerNo": "MSCU1234567", "ContType": "40HQ"}
        ],
    }

    # Patch skill runner extract_draft_json
    with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_llm_result

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Synchronous extraction
            res = await client.post(
                "/api/v1/extract/sync",
                headers=tenant_headers,
                json={
                    "mail_subject": "Booking Confirmation - MSC - Shanghai to Rotterdam",
                    "mail_body": "Please find attached booking memo BK889900.",
                    "attachments": [],
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert data["task_id"] is not None
            assert data["data"]["POLName"] == "SHANGHAI"
            assert Decimal(str(data["charged_amount"])) == Decimal("1.0000")

            # 2. Async extraction (JSON payload)
            res_async = await client.post(
                "/api/v1/extract/async",
                headers=tenant_headers,
                json={
                    "mail_subject": "Async Booking Request",
                    "mail_body": "Please book container MSCU1234567",
                    "attachments": [],
                },
            )
            assert res_async.status_code == 200
            assert "task_id" in res_async.json()

            # 3. Async extraction with file upload (/extract/async/upload)
            file_content = b"SHIPPER: ABC LOGISTICS\nPOL: NINGBO\nPOD: LONG BEACH"
            files = [("files", ("booking.txt", io.BytesIO(file_content), "text/plain"))]
            res_file = await client.post(
                "/api/v1/extract/async/upload",
                headers={"Authorization": f"Bearer {raw_key}"},
                files=files,
                data={"mail_subject": "File Upload Test"},
            )
            assert res_file.status_code == 200
            assert "task_id" in res_file.json()

            # 4. Legacy Excel files pass the same upload entry point.
            xls_files = [("files", ("packing_list.xls", io.BytesIO(b"legacy-xls-payload"), "application/vnd.ms-excel"))]
            res_xls = await client.post(
                "/api/v1/extract/async/upload",
                headers={"Authorization": f"Bearer {raw_key}"},
                files=xls_files,
                data={"mail_subject": "Legacy Excel Upload Test"},
            )
            assert res_xls.status_code == 200
            assert "task_id" in res_xls.json()


@pytest.mark.asyncio
async def test_extract_insufficient_balance_and_inactive():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="NoBalTenantTest",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("0.00"),
            unit_price=Decimal("5.00"),
        )

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Insufficient balance should return 402 Payment Required
        res = await client.post(
            "/api/v1/extract/sync",
            headers=tenant_headers,
            json={"mail_subject": "Test", "mail_body": "Test"},
        )
        assert res.status_code == 402

        # 2. Deactivate tenant -> should return 401 Unauthorized
        async with AsyncSessionLocal() as db:
            t = await db.get(Tenant, tenant.id)
            t.is_active = False
            await db.commit()

        res_inactive = await client.post(
            "/api/v1/extract/sync",
            headers=tenant_headers,
            json={"mail_subject": "Test", "mail_body": "Test"},
        )
        assert res_inactive.status_code == 401
