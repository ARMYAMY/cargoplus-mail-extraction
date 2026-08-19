from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import uuid
import socket
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.tenant import Tenant
from app.models.task import EmailTask
from app.services.billing_service import BillingService
from app.services.queue_service import task_queue
from app.services.storage_service import StorageService
from app.services.webhook_service import is_safe_webhook_url
from app.core.parser import compress_text_content


@pytest.mark.asyncio
async def test_billing_idempotency_prevents_double_charge():
    """Verify that calling deduct_for_task_success multiple times only charges once."""
    await init_db()
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="幂等测试企业",
            balance=Decimal("10.0000"),
            unit_price=Decimal("0.5000"),
        )
        db.add(tenant)
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            input_type="PAYLOAD",
            status="SUCCESS",
            is_charged=False,
            charged_amount=Decimal("0.0000"),
        )
        db.add(task)
        await db.commit()

        # 1. First deduction
        tx1 = await BillingService.deduct_for_task_success(db, t_id, task_id)
        assert tx1 is not None
        assert tx1.balance_after == Decimal("9.5000")

        # 2. Second deduction attempt (same task) -> should return None and not deduct
        tx2 = await BillingService.deduct_for_task_success(db, t_id, task_id)
        assert tx2 is None

        # 3. Verify final balance in DB is still 9.5000
        stmt = select(Tenant).where(Tenant.id == t_id)
        res = await db.execute(stmt)
        updated_tenant = res.scalar_one()
        assert updated_tenant.balance == Decimal("9.5000")


@pytest.mark.asyncio
async def test_recharge_negative_or_zero_amount_rejection():
    """Verify that negative or zero recharge amounts are strictly rejected."""
    await init_db()
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="充值校验企业",
            balance=Decimal("10.0000"),
            unit_price=Decimal("0.5000"),
        )
        db.add(tenant)
        await db.commit()

        # Negative amount
        with pytest.raises(ValueError):
            await BillingService.recharge_balance(db, t_id, Decimal("-5.0000"))

        # Zero amount
        with pytest.raises(ValueError):
            await BillingService.recharge_balance(db, t_id, Decimal("0.0000"))


@pytest.mark.asyncio
async def test_auth_registration_and_login_validation():
    """Verify user registration, correct password auth, and wrong password rejection."""
    await init_db()
    unique_email = f"user_{uuid.uuid4().hex[:6]}@freight.com"
    pwd = "SecurePassword123"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Register new tenant (Default is_active=False pending review)
        reg_res = await ac.post(
            "/api/v1/auth/register",
            json={
                "company_name": "宏洋海运代理有限公司",
                "contact_email": unique_email,
                "password": pwd,
            },
        )
        assert reg_res.status_code == 200
        data = reg_res.json()["data"]
        tenant_id = data["tenant_id"]
        assert "api_key" in data
        assert data["balance"] == 50.0
        assert data["is_active"] is False

        # 2. Login before admin approval -> 403 Forbidden
        unapproved_login = await ac.post(
            "/api/v1/auth/login",
            json={
                "account": unique_email,
                "password": pwd,
            },
        )
        assert unapproved_login.status_code == 403
        assert unapproved_login.json()["detail"]["code"] == 40301

        # 3. Admin approves tenant and updates config (unit_price & max_concurrency)
        admin_token = settings.ADMIN_SECRET_KEY or "cargo-plus-admin-secret-2026"
        admin_headers = {"X-Admin-Secret": admin_token}

        approve_res = await ac.put(
            f"/admin/tenants/{tenant_id}/status?is_active=true",
            headers=admin_headers,
        )
        assert approve_res.status_code == 200

        config_res = await ac.put(
            f"/admin/tenants/{tenant_id}",
            headers=admin_headers,
            json={
                "unit_price": 0.35,
                "max_concurrency": 30,
            },
        )
        assert config_res.status_code == 200
        assert float(config_res.json()["unit_price"]) == 0.35
        assert config_res.json()["max_concurrency"] == 30

        # 4. Login with wrong password -> 401
        fail_login = await ac.post(
            "/api/v1/auth/login",
            json={
                "account": unique_email,
                "password": "WrongPassword",
            },
        )
        assert fail_login.status_code == 401
        assert fail_login.json()["detail"]["code"] == 40103

        # 5. Login with correct password after approval -> 200
        succ_login = await ac.post(
            "/api/v1/auth/login",
            json={
                "account": unique_email,
                "password": pwd,
            },
        )
        assert succ_login.status_code == 200
        assert succ_login.json()["data"]["tenant_name"] == "宏洋海运代理有限公司"
        assert succ_login.json()["data"]["unit_price"] == 0.35



@pytest.mark.asyncio
async def test_crash_recovery_resets_processing_tasks():
    """Verify that uncompleted tasks in PROCESSING status are recovered to PENDING on startup."""
    await init_db()
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="崩溃恢复测试租户",
            balance=Decimal("20.0000"),
            unit_price=Decimal("0.5000"),
        )
        db.add(tenant)
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            input_type="PAYLOAD",
            status="PROCESSING",  # Stuck due to prior crash
            is_charged=False,
            charged_amount=Decimal("0.0000"),
        )
        db.add(task)
        await db.commit()

    # Trigger recovery
    await task_queue._recover_uncompleted_tasks()

    # Check task status in DB is reset to PENDING
    async with AsyncSessionLocal() as db:
        stmt = select(EmailTask).where(EmailTask.id == task_id)
        res = await db.execute(stmt)
        recovered_task = res.scalar_one()
        assert recovered_task.status == "PENDING"


def test_storage_pruning_logic(tmp_path: Path):
    """Verify that StorageService only deletes files older than cutoff days."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True)

    # 1. Create an old file (> 95 days old)
    old_file = upload_dir / "old_doc.pdf"
    old_file.write_text("old content")
    old_mtime = (datetime.now(timezone.utc) - timedelta(days=95)).timestamp()
    os.utime(str(old_file), (old_mtime, old_mtime))

    # 2. Create a recent file (1 day old)
    new_file = upload_dir / "recent_doc.pdf"
    new_file.write_text("new content")

    # Temporarily point settings.UPLOAD_DIR to tmp_path
    original_uploads = settings.UPLOAD_DIR
    settings.UPLOAD_DIR = str(upload_dir)

    try:
        deleted = StorageService.prune_expired_uploads(days=90)
        assert deleted == 1
        assert not old_file.exists()
        assert new_file.exists()
    finally:
        settings.UPLOAD_DIR = original_uploads


def test_smart_document_compression():
    """Verify that compress_text_content compresses huge text while preserving cargo keywords."""
    large_noise = "This is general non-cargo conversational chatter that takes up words.\n" * 200
    cargo_key_line = "Booking No: BKG998822 Container: COSU1234567 Shipper: ABC LOGISTICS POL: CNSHA POD: USLAX"
    huge_text = f"{large_noise}\n{cargo_key_line}\n{large_noise}"

    compressed = compress_text_content(huge_text, max_chars=1000)
    assert len(compressed) <= 1000
    assert "BKG998822" in compressed
    assert "COSU1234567" in compressed
    assert "CNSHA" in compressed


def test_webhook_ssrf_blocking(monkeypatch):
    """Verify SSRF protection blocks private, loopback, and metadata URLs while allowing public HTTPS."""
    def fake_getaddrinfo(hostname, port, **_kwargs):
        address = "93.184.216.34" if hostname == "safe.example" else hostname
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    # Forbidden dangerous URLs
    assert not is_safe_webhook_url("http://127.0.0.1:8000/callback")
    assert not is_safe_webhook_url("http://localhost:5000/webhook")
    assert not is_safe_webhook_url("http://169.254.169.254/latest/meta-data")
    assert not is_safe_webhook_url("http://10.0.0.1:9000/internal")
    assert not is_safe_webhook_url("http://192.168.1.100/notify")
    assert not is_safe_webhook_url("ftp://example.com/file")
    assert not is_safe_webhook_url("javascript:alert(1)")

    # Allowed safe public URLs
    assert is_safe_webhook_url("https://safe.example/abc-123")
