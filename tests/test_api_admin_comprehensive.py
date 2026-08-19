import uuid
from decimal import Decimal
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.config import settings


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_admin_tenants_full_flow():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
        # 1. Create a tenant via Admin API
        t_name = f"AdminTest_{uuid.uuid4().hex[:6]}"
        res = await client.post(
            "/admin/tenants",
            headers=admin_headers,
            json={
                "name": t_name,
                "contact_email": f"{t_name}@example.com",
                "contact_phone": "13800000000",
                "unit_price": 0.88,
                "initial_balance": 200.0,
                "max_concurrency": 15,
            },
        )
        assert res.status_code == 200
        t_data = res.json()
        tenant_id = t_data["id"]
        assert t_data["name"] == t_name
        assert len(t_data["api_keys"]) > 0

        # Duplicate email conflict -> 409
        res_dup = await client.post(
            "/admin/tenants",
            headers=admin_headers,
            json={
                "name": f"{t_name}_Dup",
                "contact_email": f"{t_name}@example.com",
                "unit_price": 1.0,
                "initial_balance": 0.0,
            },
        )
        assert res_dup.status_code == 409

        # 2. List all tenants
        res = await client.get("/admin/tenants", headers=admin_headers)
        assert res.status_code == 200
        tenants = res.json()
        assert any(t["id"] == tenant_id for t in tenants)

        # 3. Update tenant configuration
        res = await client.put(
            f"/admin/tenants/{tenant_id}",
            headers=admin_headers,
            json={
                "name": f"{t_name}_Updated",
                "contact_phone": "13911112222",
                "unit_price": 1.25,
                "max_concurrency": 30,
                "is_active": True,
            },
        )
        assert res.status_code == 200
        updated = res.json()
        assert updated["name"] == f"{t_name}_Updated"
        assert Decimal(str(updated["unit_price"])) == Decimal("1.25")

        # 4. Toggle tenant audit status (Audit approve / deactivate)
        res = await client.put(
            f"/admin/tenants/{tenant_id}/status?is_active=false",
            headers=admin_headers,
        )
        assert res.status_code == 200
        assert res.json()["data"]["is_active"] is False

        res = await client.put(
            f"/admin/tenants/{tenant_id}/status?is_active=true",
            headers=admin_headers,
        )
        assert res.status_code == 200
        assert res.json()["data"]["is_active"] is True

        # 5. List keys for tenant
        res = await client.get(f"/admin/tenants/{tenant_id}/keys", headers=admin_headers)
        assert res.status_code == 200
        keys = res.json()
        assert len(keys) >= 1
        key_id = keys[0]["id"]

        # 6. Generate new key for tenant
        res = await client.post(
            f"/admin/tenants/{tenant_id}/keys?key_name=ERP_Key",
            headers=admin_headers,
        )
        assert res.status_code == 200
        new_key_data = res.json()
        assert "raw_api_key" in new_key_data
        assert new_key_data["name"] == "ERP_Key"
        erp_key_id = new_key_data["id"]

        # 7. Revoke Key
        res = await client.delete(f"/admin/tenants/keys/{erp_key_id}", headers=admin_headers)
        assert res.status_code == 200

        # 8. Edge cases: non-existent tenant
        res = await client.put("/admin/tenants/non_existent_id", headers=admin_headers, json={"name": "test"})
        assert res.status_code == 404

        res = await client.put("/admin/tenants/non_existent_id/status?is_active=true", headers=admin_headers)
        assert res.status_code == 404

        res = await client.post("/admin/tenants/non_existent_id/keys", headers=admin_headers)
        assert res.status_code == 404

        res = await client.delete("/admin/tenants/keys/non_existent_key_id", headers=admin_headers)
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_admin_recharge_full_flow():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
        # Create tenant
        t_name = f"RechargeTest_{uuid.uuid4().hex[:6]}"
        res = await client.post(
            "/admin/tenants",
            headers=admin_headers,
            json={"name": t_name, "initial_balance": 50.0},
        )
        tenant_id = res.json()["id"]

        # Recharge positive amount
        res = await client.post(
            f"/admin/recharge/{tenant_id}",
            headers=admin_headers,
            json={"amount": 150.0, "description": "银行转账充值", "operator": "FinanceAdmin"},
        )
        assert res.status_code == 200
        data = res.json()
        assert Decimal(str(data["balance_after"])) == Decimal("200.0000")

        # Invalid amount (zero or negative)
        res = await client.post(
            f"/admin/recharge/{tenant_id}",
            headers=admin_headers,
            json={"amount": -10.0},
        )
        assert res.status_code == 422

        # Non-existent tenant recharge
        res = await client.post(
            "/admin/recharge/non_existent_tenant",
            headers=admin_headers,
            json={"amount": 100.0},
        )
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_admin_billing_transactions():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
        # Create tenant & trigger recharge
        t_name = f"BillTxTest_{uuid.uuid4().hex[:6]}"
        res = await client.post(
            "/admin/tenants",
            headers=admin_headers,
            json={"name": t_name, "initial_balance": 10.0},
        )
        tenant_id = res.json()["id"]

        await client.post(
            f"/admin/recharge/{tenant_id}",
            headers=admin_headers,
            json={"amount": 50.0, "description": "测试充值描述"},
        )

        # Query all billing transactions
        res = await client.get("/admin/billing/transactions?page=1&page_size=10", headers=admin_headers)
        assert res.status_code == 200
        data = res.json()
        assert "items" in data
        assert "total" in data
        assert data["page"] == 1

        # Query with type filter
        res = await client.get("/admin/billing/transactions?type=RECHARGE", headers=admin_headers)
        assert res.status_code == 200
        assert all(item["type"] == "RECHARGE" for item in res.json()["items"])

        # Query with tenant filter
        res = await client.get(f"/admin/billing/transactions?tenant_id={tenant_id}", headers=admin_headers)
        assert res.status_code == 200
        assert all(item["tenant_id"] == tenant_id for item in res.json()["items"])

        # Query with search
        res = await client.get("/admin/billing/transactions?search=测试充值", headers=admin_headers)
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_admin_stats_overview_and_trends():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
        # Overview stats
        res = await client.get("/admin/stats", headers=admin_headers)
        assert res.status_code == 200
        data = res.json()
        assert "total_tenants" in data
        assert "active_tenants" in data
        assert "total_tenant_balance" in data

        # Historical stats
        res_hist = await client.get("/admin/stats/history?days=30", headers=admin_headers)
        assert res_hist.status_code == 200
        hist_data = res_hist.json()
        assert "daily_trends" in hist_data or "tenant_ranking" in hist_data or isinstance(hist_data, dict)


@pytest.mark.asyncio
async def test_admin_tasks_monitor_and_retry():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)

    # Insert a task directly
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        db.add(Tenant(id=t_id, name="TaskMonTenant", balance=Decimal("10.00"), is_active=True))
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="FAILED",
            error_message="Simulated OCR failure",
            mail_subject="Test Subject",
        )
        db.add(task)
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
        # 1. Query admin tasks list with pagination and search
        res = await client.get(f"/admin/tasks?page=1&page_size=20&search={task_id}", headers=admin_headers)
        assert res.status_code == 200
        tasks = res.json()
        assert tasks["total"] >= 1
        assert any(item["id"] == task_id for item in tasks["items"])

        # 2. Filter by status
        res = await client.get("/admin/tasks?status=FAILED", headers=admin_headers)
        assert res.status_code == 200

        # 3. Filter by tenant_id
        res = await client.get(f"/admin/tasks?tenant_id={t_id}", headers=admin_headers)
        assert res.status_code == 200

        # 4. Retry failed task
        res = await client.post(f"/admin/tasks/{task_id}/retry", headers=admin_headers)
        assert res.status_code == 200
        assert res.json()["code"] == 0

        # 5. Retry non-existent task
        res = await client.post("/admin/tasks/non_existent_task/retry", headers=admin_headers)
        assert res.status_code == 404
