from decimal import Decimal
import json
import uuid
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import ApiKey, Tenant
from app.services.auth_service import hash_api_key
from app.services.webhook_service import generate_webhook_signature


@pytest.mark.asyncio
async def test_api_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_api_auth_and_insufficient_balance():
    await init_db()
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key = f"cg_test_{uuid.uuid4().hex[:12]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="零余额客户",
            balance=Decimal("0.0000"),
            unit_price=Decimal("0.5000"),
        )
        db.add(tenant)
        await db.commit()

        api_key = ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            name="No Money Key",
            key_prefix="cg_test",
            key_hash=hash_api_key(raw_key),
            api_secret="secret_123",
        )
        db.add(api_key)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Test missing key -> 401
        res_no_key = await ac.post("/api/v1/extract/async", json={"mail_body": "test"})
        assert res_no_key.status_code == 401

        # 2. Test insufficient balance -> 402 Payment Required
        headers = {"Authorization": f"Bearer {raw_key}"}
        res_no_bal = await ac.post("/api/v1/extract/async", json={"mail_body": "test"}, headers=headers)
        assert res_no_bal.status_code == 402
        assert res_no_bal.json()["detail"]["code"] == 40201


@pytest.mark.asyncio
async def test_sync_extraction_flow_mock_llm():
    await init_db()
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key = f"cg_test_{uuid.uuid4().hex[:12]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="充值客户",
            balance=Decimal("10.0000"),
            unit_price=Decimal("0.5000"),
        )
        db.add(tenant)
        await db.commit()

        api_key = ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            name="Funded Key",
            key_prefix="cg_test",
            key_hash=hash_api_key(raw_key),
            api_secret="secret_456",
        )
        db.add(api_key)
        await db.commit()

    mock_llm_response = {
        "ShipperName": "SHENZHEN FORWARDER CO.",
        "ConsigneeName": "MELBOURNE IMPORTS",
        "POLName": "YANTIAN",
        "PODName": "MELBOURNE",
        "GoodsName": "FURNITURE 家具",
        "Packages": "500 CARTONS",
        "GrossWeight": "10,000 KGS",
        "Volume": "40 CBM",
        "ContainerInfo": [
            {
                "ContainerNo": "TGHU1234567",
                "SealNo": "SL123",
                "ContSize": "40",
                "ContType": "HQ",
                "KGS": "10000 KGS",
                "PCS": "500 CARTONS",
                "CBM": "40 CBM",
            }
        ]
    }

    with patch("app.core.skill_runner.default_skill_runner.call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = json.dumps(mock_llm_response, ensure_ascii=False)

        headers = {"Authorization": f"Bearer {raw_key}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post(
                "/api/v1/extract/sync",
                json={
                    "mail_subject": "Booking Confirmation",
                    "mail_body": "Please find attached booking.",
                    "attachments": [],
                },
                headers=headers,
            )
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["code"] == 0
            assert data["status"] == "SUCCESS"
            assert data["charged_amount"] == 0.50
            assert data["data"]["Packages"] == "500"
            assert data["data"]["PackagesUnit"] == "CARTONS"
            assert data["data"]["GoodsName"] == "FURNITURE"
            assert data["data"]["GoodsNameCN"] == "家具"
            assert data["data"]["ContainerInfo"][0]["ContSize"] == "40"
            assert data["data"]["ContainerInfo"][0]["ContType"] == "HQ"


@pytest.mark.asyncio
async def test_admin_tenant_management_and_recharge():
    await init_db()
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Create tenant via admin API
        create_res = await ac.post(
            "/admin/tenants",
            json={
                "name": "新签约货代公司",
                "contact_email": "forwarder@company.com",
                "contact_phone": "13912345678",
                "unit_price": 0.35,
                "initial_balance": 200.0,
                "max_concurrency": 30,
            },
            headers=admin_headers,
        )
        assert create_res.status_code == 200
        tenant_data = create_res.json()
        assert tenant_data["name"] == "新签约货代公司"
        assert float(tenant_data["balance"]) == 200.0
        assert float(tenant_data["unit_price"]) == 0.35
        assert len(tenant_data["api_keys"]) == 1
        assert tenant_data["api_keys"][0]["raw_api_key"] is not None

        tenant_id = tenant_data["id"]

        # 2. Recharge tenant balance
        recharge_res = await ac.post(
            f"/admin/recharge/{tenant_id}",
            json={
                "amount": 500.0,
                "description": "银行转账预付款",
                "operator": "FINANCE_ADMIN",
            },
            headers=admin_headers,
        )
        assert recharge_res.status_code == 200
        recharge_data = recharge_res.json()
        assert float(recharge_data["amount"]) == 500.0
        assert float(recharge_data["balance_after"]) == 700.0

        # 3. Update unit price
        price_res = await ac.put(
            f"/admin/tenants/{tenant_id}/unit-price",
            json={"unit_price": 0.30},
            headers=admin_headers,
        )
        assert price_res.status_code == 200


@pytest.mark.asyncio
async def test_webhook_signature_calculation():
    secret = "test_webhook_secret_key"
    timestamp = 1723971234567
    payload = json.dumps({"event": "task.completed", "task_id": "task_123"}, ensure_ascii=False)

    sig1 = generate_webhook_signature(secret, timestamp, payload)
    sig2 = generate_webhook_signature(secret, timestamp, payload)
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA-256 hex length


@pytest.mark.asyncio
async def test_admin_historical_dashboard_stats_shape_and_rate():
    await init_db()
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/stats/history?days=30", headers=admin_headers)
        invalid_period = await ac.get("/admin/stats/history?days=3", headers=admin_headers)

    assert response.status_code == 200
    assert invalid_period.status_code == 422
    payload = response.json()
    assert set(payload) == {"lifetime", "period", "history", "tenant_rankings"}
    assert len(payload["history"]) == 30
    assert payload["period"]["days"] == 30
    assert payload["history"][0]["date"] == payload["period"]["start_date"]
    assert payload["history"][-1]["date"] == payload["period"]["end_date"]

    lifetime = payload["lifetime"]
    assert lifetime["total"] >= lifetime["success"] + lifetime["failed"]
    completed = lifetime["success"] + lifetime["failed"]
    expected_rate = round(lifetime["success"] / completed * 100, 1) if completed else 0.0
    assert lifetime["success_rate"] == expected_rate
    assert lifetime["revenue"] >= 0
