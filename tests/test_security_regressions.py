import asyncio
from decimal import Decimal
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.v1.billing import _csv_safe
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.billing import BillingTransaction
from app.models.task import EmailTask
from app.models.tenant import ApiKey, Tenant
from app.services.auth_service import (
    create_access_token,
    hash_api_key,
    hash_password,
    verify_access_token,
    verify_password,
)
from app.services.billing_service import BillingService


async def _create_tenant_with_key(*, balance: str = "10.0000", active: bool = True):
    tenant_id = f"tenant_{uuid.uuid4().hex[:10]}"
    raw_key = f"cg_test_{uuid.uuid4().hex}"
    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=tenant_id,
            name=f"Security Tenant {tenant_id}",
            balance=Decimal(balance),
            unit_price=Decimal("0.5000"),
            is_active=active,
        )
        db.add(tenant)
        db.add(
            ApiKey(
                id=f"key_{uuid.uuid4().hex[:10]}",
                tenant_id=tenant_id,
                name="Security Test Key",
                key_prefix="cg_test",
                key_hash=hash_api_key(raw_key),
                api_secret=uuid.uuid4().hex,
            )
        )
        await db.commit()
    return tenant_id, raw_key


def test_password_hashes_are_salted_and_legacy_safe():
    first = hash_password("Correct Horse Battery Staple")
    second = hash_password("Correct Horse Battery Staple")
    assert first != second
    assert verify_password("Correct Horse Battery Staple", first)
    assert not verify_password("wrong", first)


def test_access_token_expiry_and_signature_validation():
    token = create_access_token("tenant_abc", expires_in=60)
    assert verify_access_token(token, expected_role="tenant")["sub"] == "tenant_abc"
    assert verify_access_token(token + "tampered") is None
    assert verify_access_token(create_access_token("tenant_abc", expires_in=-1)) is None


@pytest.mark.asyncio
async def test_tenant_id_is_not_an_authentication_token():
    await init_db()
    tenant_id, _ = await _create_tenant_with_key()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        forged = await client.get(
            "/api/v1/billing/summary",
            headers={"Authorization": f"Bearer {tenant_id}"},
        )
        assert forged.status_code == 401

        valid = await client.get(
            "/api/v1/billing/summary",
            headers={"Authorization": f"Bearer {create_access_token(tenant_id)}"},
        )
        assert valid.status_code == 200
        assert valid.json()["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_admin_requires_explicit_auth_and_login_returns_session_token():
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/admin/stats")
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store"
        assert denied.headers["x-frame-options"] == "DENY"

        login = await client.post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": settings.ADMIN_SECRET_KEY},
        )
        assert login.status_code == 200
        token = login.json()["data"]["admin_token"]
        assert token.startswith("cgs_")
        assert token != settings.ADMIN_SECRET_KEY

        allowed = await client.get(
            "/admin/stats",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_inactive_tenant_cannot_log_in_with_api_key():
    await init_db()
    _, raw_key = await _create_tenant_with_key(active=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"account": raw_key},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_concurrent_billing_cannot_overdraw_or_double_charge():
    await init_db()
    tenant_id, _ = await _create_tenant_with_key(balance="0.5000")
    task_ids = [f"task_{uuid.uuid4().hex[:12]}" for _ in range(2)]
    async with AsyncSessionLocal() as db:
        db.add_all(
            [EmailTask(id=task_id, tenant_id=tenant_id, status="SUCCESS") for task_id in task_ids]
        )
        await db.commit()

    async def charge(task_id):
        async with AsyncSessionLocal() as db:
            return await BillingService.deduct_for_task_success(db, tenant_id, task_id)

    results = await asyncio.gather(*(charge(task_id) for task_id in task_ids))
    assert sum(result is not None for result in results) == 1

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        tx_count = (
            await db.execute(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.tenant_id == tenant_id,
                    BillingTransaction.type == "DEDUCTION",
                )
            )
        ).scalar_one()
        assert tenant.balance == Decimal("0.0000")
        assert tx_count == 1


@pytest.mark.asyncio
async def test_recharge_rejects_database_overflow_and_preserves_balance():
    await init_db()
    tenant_id, _ = await _create_tenant_with_key(balance="99999999.5000")
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        too_large = await client.post(
            f"/admin/recharge/{tenant_id}",
            headers=admin_headers,
            json={"amount": "1e76", "description": "must be rejected"},
        )
        would_overflow_balance = await client.post(
            f"/admin/recharge/{tenant_id}",
            headers=admin_headers,
            json={"amount": "1.0000", "description": "must also be rejected"},
        )

    assert too_large.status_code == 422
    assert would_overflow_balance.status_code == 422
    assert "maximum account balance" in would_overflow_balance.json()["detail"]["message"]

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        recharge_count = (
            await db.execute(
                select(func.count(BillingTransaction.id)).where(
                    BillingTransaction.tenant_id == tenant_id,
                    BillingTransaction.type == "RECHARGE",
                )
            )
        ).scalar_one()
        assert tenant.balance == Decimal("99999999.5000")
        assert recharge_count == 0


@pytest.mark.asyncio
async def test_sqlite_money_trigger_rejects_direct_out_of_range_update():
    await init_db()
    tenant_id, _ = await _create_tenant_with_key()

    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="tenant money value outside business limits"):
            await db.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(unit_price=Decimal("100.0001"))
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_sqlite_concurrency_trigger_rejects_direct_out_of_range_update():
    await init_db()
    tenant_id, _ = await _create_tenant_with_key()

    async with AsyncSessionLocal() as db:
        with pytest.raises(IntegrityError, match="tenant concurrency .*business limits"):
            await db.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(max_concurrency=31)
            )
        await db.rollback()

        with pytest.raises(IntegrityError, match="tenant concurrency must be an integer"):
            await db.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(max_concurrency=1.5)
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_billing_transactions_endpoint_is_paginated_and_serializable():
    await init_db()
    tenant_id, raw_key = await _create_tenant_with_key(balance="10.0000")
    async with AsyncSessionLocal() as db:
        await BillingService.recharge_balance(db, tenant_id, Decimal("5.0000"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/billing/transactions?page=1&page_size=10",
            headers={"Authorization": f"Bearer {raw_key}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["page_size"] == 10
    assert payload["total_pages"] == 1
    assert payload["items"][0]["type"] == "RECHARGE"
    assert payload["items"][0]["amount"] == "5.0000"


@pytest.mark.asyncio
async def test_private_callback_and_unsupported_upload_are_rejected_without_artifacts():
    await init_db()
    _, raw_key = await _create_tenant_with_key()
    headers = {"Authorization": f"Bearer {raw_key}"}
    before = set(settings.uploads_path.iterdir())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        callback_response = await client.post(
            "/api/v1/extract/async",
            headers=headers,
            json={
                "mail_body": "cargo",
                "callback_url": "http://127.0.0.1/internal",
            },
        )
        assert callback_response.status_code == 422

        upload_response = await client.post(
            "/api/v1/extract/async/upload",
            headers=headers,
            files={"files": ("payload.exe", b"not executable", "application/octet-stream")},
        )
        assert upload_response.status_code == 415

    assert set(settings.uploads_path.iterdir()) == before


def test_csv_formula_injection_is_neutralized():
    assert _csv_safe("=HYPERLINK(\"https://evil.example\")").startswith("'=")
    assert _csv_safe("ordinary text") == "ordinary text"


@pytest.mark.asyncio
async def test_atomic_reservation_blocks_overcommitted_queue_submissions():
    await init_db()
    tenant_id, raw_key = await _create_tenant_with_key(balance="0.5000")
    headers = {"Authorization": f"Bearer {raw_key}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/api/v1/extract/async",
            headers=headers,
            json={"mail_body": "first cargo request"},
        )
        second = await client.post(
            "/api/v1/extract/async",
            headers=headers,
            json={"mail_body": "second cargo request"},
        )
    assert first.status_code == 200
    assert second.status_code == 402

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        task = (
            await db.execute(select(EmailTask).where(EmailTask.id == first.json()["task_id"]))
        ).scalar_one()
        assert tenant.balance == Decimal("0.5000")
        assert tenant.reserved_balance == Decimal("0.5000")
        assert task.is_reserved is True


@pytest.mark.asyncio
async def test_failed_extraction_releases_reservation_without_charging():
    await init_db()
    tenant_id, raw_key = await _create_tenant_with_key(balance="0.5000")
    headers = {"Authorization": f"Bearer {raw_key}"}
    with patch(
        "app.core.skill_runner.default_skill_runner.call_llm",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upstream unavailable"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/extract/sync",
                headers=headers,
                json={"mail_body": "cargo request"},
            )
    assert response.status_code == 500

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        task = (
            await db.execute(select(EmailTask).where(EmailTask.id == response.json()["detail"]["task_id"]))
        ).scalar_one()
        assert tenant.balance == Decimal("0.5000")
        assert tenant.reserved_balance == Decimal("0.0000")
        assert task.is_reserved is False
        assert task.is_charged is False
