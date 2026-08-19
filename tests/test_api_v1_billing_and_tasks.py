import json
import uuid
from decimal import Decimal
from datetime import datetime, timezone
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.auth_service import hash_password, generate_api_key_and_secret


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
async def test_tenant_billing_and_statements():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="BillingTenantTest",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("200.00"),
            unit_price=Decimal("0.50"),
        )

        # Insert some transactions (Recharge & Deductions)
        t1 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant.id,
            type="RECHARGE",
            amount=Decimal("50.00"),
            balance_before=Decimal("200.00"),
            balance_after=Decimal("250.00"),
            description="线上充值",
        )
        t2 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant.id,
            type="DEDUCT",
            amount=Decimal("0.50"),
            balance_before=Decimal("250.00"),
            balance_after=Decimal("249.50"),
            description="邮件提取扣费",
        )
        db.add_all([t1, t2])
        await db.commit()

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Billing Summary
        res = await client.get("/api/v1/billing/summary", headers=tenant_headers)
        assert res.status_code == 200
        summary = res.json()
        assert "current_balance" in summary
        assert "available_balance" in summary
        assert "unit_price" in summary
        assert "total_tasks_charged" in summary

        # 2. Billing Transactions list with pagination
        res = await client.get("/api/v1/billing/transactions?page=1&page_size=10", headers=tenant_headers)
        assert res.status_code == 200
        tx_data = res.json()
        assert tx_data["total"] >= 2
        assert len(tx_data["items"]) >= 2

        # 3. Filter transactions by type
        res = await client.get("/api/v1/billing/transactions?type=DEDUCT", headers=tenant_headers)
        assert res.status_code == 200
        assert all(t["type"] == "DEDUCT" for t in res.json()["items"])

        # 4. Daily Statements
        res = await client.get("/api/v1/billing/statements/daily?days=30&page=1&page_size=10", headers=tenant_headers)
        assert res.status_code == 200
        statements = res.json()
        assert "items" in statements
        assert "total" in statements

        # 5. Export CSV
        res_csv = await client.get("/api/v1/billing/export-csv", headers=tenant_headers)
        assert res_csv.status_code == 200
        assert "text/csv" in res_csv.headers.get("content-type", "")
        csv_text = res_csv.text
        assert "交易流水号" in csv_text or "Transaction ID" in csv_text or "交易时间" in csv_text


@pytest.mark.asyncio
async def test_tenant_tasks_endpoints():
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"
    other_t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    task_id_1 = f"task_{uuid.uuid4().hex[:8]}"
    task_id_other = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant, raw_key = await create_test_tenant(
            db=db,
            name="TasksTenantTest",
            email=f"{t_id}@example.com",
            initial_balance=Decimal("100.00"),
            unit_price=Decimal("0.50"),
        )

        other_tenant, _ = await create_test_tenant(
            db=db,
            name="OtherTenantTest",
            email=f"{other_t_id}@example.com",
            initial_balance=Decimal("100.00"),
            unit_price=Decimal("0.50"),
        )

        t1 = EmailTask(
            id=task_id_1,
            tenant_id=tenant.id,
            status="SUCCESS",
            mail_subject="Booking MSC test",
            result_json=json.dumps({"shipment": {"pol": "Yantian", "pod": "Hamburg"}}),
        )
        t2 = EmailTask(
            id=task_id_other,
            tenant_id=other_tenant.id,
            status="FAILED",
            mail_subject="Secret Task",
        )
        db.add_all([t1, t2])
        await db.commit()

    tenant_headers = {"Authorization": f"Bearer {raw_key}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. List tenant's own tasks
        res = await client.get("/api/v1/tasks?page=1&page_size=10", headers=tenant_headers)
        assert res.status_code == 200
        tasks = res.json()
        assert tasks["total"] >= 1
        assert any(t["id"] == task_id_1 for t in tasks["items"])
        # Should not leak other tenant's task
        assert not any(t["id"] == task_id_other for t in tasks["items"])

        # 2. Filter by status
        res = await client.get("/api/v1/tasks?status=SUCCESS", headers=tenant_headers)
        assert res.status_code == 200
        assert all(t["status"] == "SUCCESS" for t in res.json()["items"])

        # 3. Get single task
        res = await client.get(f"/api/v1/tasks/{task_id_1}", headers=tenant_headers)
        assert res.status_code == 200
        assert res.json()["id"] == task_id_1
        assert res.json()["result_json"]["shipment"]["pol"] == "Yantian"

        # 4. Attempt to access other tenant's task (Forbidden / 404)
        res_other = await client.get(f"/api/v1/tasks/{task_id_other}", headers=tenant_headers)
        assert res_other.status_code == 404

        # 5. Non-existent task
        res_none = await client.get("/api/v1/tasks/non_existent_task_id", headers=tenant_headers)
        assert res_none.status_code == 404
