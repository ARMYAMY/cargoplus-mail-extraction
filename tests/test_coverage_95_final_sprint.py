import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import socket
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile

from app.config import settings
from app.database import AsyncSessionLocal, init_db, get_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask, WebhookLog
from app.models.billing import BillingTransaction
from app.services.auth_service import (
    authenticate_api_key,
    create_access_token,
    generate_api_key_and_secret,
    hash_api_key,
    hash_password,
    password_needs_rehash,
    verify_access_token,
    verify_password,
)
from app.services.billing_service import BillingService
from app.services.extraction_service import ExtractionService
from app.services.queue_service import TaskQueueManager
from app.services.storage_service import StorageService
from app.services.webhook_service import is_safe_webhook_url, send_webhook_notification
from app.core.money import validate_money, MAX_ACCOUNT_BALANCE, MAX_UNIT_PRICE, MIN_UNIT_PRICE
from app.core.normalizer import CargoNormalizer
from app.core.validator import CargoValidator
from app.core.skill_runner import SkillRunner


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


def test_auth_service_all_token_and_pwd_branches():
    # 1. verify_access_token expired token
    expired_token = create_access_token("test_user", expires_in=-10)
    assert verify_access_token(expired_token) is None

    # 2. verify_access_token invalid signature / malformed token
    assert verify_access_token("invalid.jwt.token.string") is None
    assert verify_access_token("cgs_malformed_token") is None

    # 3. verify_access_token role mismatch
    user_token = create_access_token("tenant_001", role="tenant")
    assert verify_access_token(user_token, expected_role="admin") is None
    assert verify_access_token(user_token, expected_role="tenant") is not None

    # 4. verify_password with empty / corrupted hash
    assert verify_password("pwd", "") is False
    assert verify_password("pwd", "corrupted_hash") is False


def test_webhook_security_and_signing():
    # 1. is_safe_webhook_url with invalid scheme
    assert is_safe_webhook_url("ftp://example.com/webhook") is False
    assert is_safe_webhook_url("file:///etc/passwd") is False
    assert is_safe_webhook_url("javascript:alert(1)") is False

    # 2. is_safe_webhook_url with missing host
    assert is_safe_webhook_url("https://") is False
    assert is_safe_webhook_url("") is False

    # 3. is_safe_webhook_url private IP ranges
    assert is_safe_webhook_url("http://10.0.0.1/hook") is False
    assert is_safe_webhook_url("http://172.16.0.1/hook") is False
    assert is_safe_webhook_url("http://169.254.169.254/latest") is False
    assert is_safe_webhook_url("http://127.0.0.1/hook") is False
    assert is_safe_webhook_url("http://localhost/hook") is False

    # 4. is_safe_webhook_url DNS resolution failure
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("DNS lookup failed")):
        assert is_safe_webhook_url("https://nonexistentdomain123456789.com/hook") is False


@pytest.mark.asyncio
async def test_queue_service_worker_execution_flow():
    manager = TaskQueueManager()
    await manager.start()
    # Calling start second time (already running branch)
    await manager.start()

    t_id = f"tenant_{uuid.uuid4().hex[:6]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="WorkerFlowTenant", balance=Decimal("100.00"), is_active=True)
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            raw_input_json=json.dumps({"mail_subject": "Worker Flow", "mail_body": "POL: SHANGHAI POD: HAMBURG", "attachments": []}),
        )
        db.add(tenant)
        db.add(task)
        await db.commit()

    with patch("app.services.extraction_service.ExtractionService.process_task", new_callable=AsyncMock):
        await manager.enqueue(task_id, t_id, "secret_key")
        await asyncio.sleep(0.1)

    await manager.stop()


def test_normalizer_internal_helper_branches():
    norm = CargoNormalizer()

    # 1. _as_string with Various types
    assert norm._as_string(None) == ""
    assert norm._as_string(123) == "123"
    assert norm._as_string(45.67) == "45.67"
    assert norm._as_string(True) == "True"

    # 2. _has_cjk
    assert norm._has_cjk("上海港") is True
    assert norm._has_cjk("PORT OF SHANGHAI") is False
    assert norm._has_cjk("SHANGHAI 上海") is True

    # 3. _split_goods_name with parts
    e1, c1 = norm._split_goods_name("AUTOPARTS / 汽车配件", "")
    assert e1 == "AUTOPARTS"
    assert c1 == "汽车配件"


@pytest.mark.asyncio
async def test_billing_service_transactions_and_summary():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="DailyStmtTenant", balance=Decimal("100.00"), unit_price=Decimal("1.00"), is_active=True)
        db.add(tenant)
        await db.commit()

        # Add multiple transactions across days
        tx1 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="DEDUCTION",
            amount=Decimal("1.00"),
            balance_before=Decimal("100.00"),
            balance_after=Decimal("99.00"),
            description="Daily stmt test deduction",
        )
        tx2 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="RECHARGE",
            amount=Decimal("50.00"),
            balance_before=Decimal("99.00"),
            balance_after=Decimal("149.00"),
            description="Daily stmt test recharge",
        )
        db.add_all([tx1, tx2])
        await db.commit()

        # Summary
        summary = await BillingService.get_tenant_billing_summary(db, t_id)
        assert summary["total_recharged"] == Decimal("50.00")
        assert summary["total_deducted"] == Decimal("1.00")

        # Transactions with filter
        txs_deduct = await BillingService.get_tenant_transactions(db, t_id, limit=10, transaction_type="DEDUCTION")
        assert len(txs_deduct) >= 1

        all_tx_rec = await BillingService.get_all_transactions(db, limit=10, transaction_type="RECHARGE")
        assert len(all_tx_rec) >= 1
