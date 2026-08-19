import asyncio
from decimal import Decimal
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio

from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask, WebhookLog
from app.services.auth_service import (
    hash_password,
    verify_password,
    password_needs_rehash,
    generate_api_key_and_secret,
    hash_api_key,
    create_access_token,
    verify_access_token,
    authenticate_api_key,
)
from app.services.billing_service import BillingService
from app.services.storage_service import StorageService
from app.services.webhook_service import is_safe_webhook_url, send_webhook_notification
from app.services.queue_service import TaskQueueManager, task_queue
from app.services.extraction_service import ExtractionService


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_auth_service_functions():
    async with AsyncSessionLocal() as db:
        # 1. Password hashing, verification, needs_rehash
        hashed = hash_password("MyPassword123!")
        assert verify_password("MyPassword123!", hashed) is True
        assert verify_password("WrongPassword", hashed) is False
        assert verify_password("", "") is False
        assert password_needs_rehash("legacy_hash") is True
        assert password_needs_rehash(hashed) is False

        # 2. Key generation & hashing
        raw_key, prefix, key_hash, api_secret = generate_api_key_and_secret()
        assert raw_key.startswith("cg_")
        assert hash_api_key(raw_key) == key_hash

        # 3. Access token generation and verification
        token = create_access_token("tenant_test_123", role="tenant")
        payload = verify_access_token(token, expected_role="tenant")
        assert payload is not None
        assert payload["sub"] == "tenant_test_123"

        # Invalid token verification
        assert verify_access_token("invalid_token") is None
        assert verify_access_token(token, expected_role="admin") is None

        # 4. Authenticate API Key in DB
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="AuthTestTenant", is_active=True, balance=Decimal("100.00"))
        api_key = ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            key_prefix=prefix,
            key_hash=key_hash,
            api_secret=api_secret,
            is_active=True,
        )
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        auth_res = await authenticate_api_key(db, raw_key)
        assert auth_res is not None
        assert auth_res[0].id == t_id

        # Bad key
        assert await authenticate_api_key(db, "bad_key") is None
        assert await authenticate_api_key(db, "") is None


@pytest.mark.asyncio
async def test_storage_service_prune(tmp_path):
    # Test valid days
    res = StorageService.prune_expired_uploads(days=90)
    assert isinstance(res, int)

    # Test invalid days
    with pytest.raises(ValueError):
        StorageService.prune_expired_uploads(days=0)


def test_webhook_url_safety():
    assert is_safe_webhook_url("") is False
    assert is_safe_webhook_url("ftp://invalid-scheme.com") is False
    assert is_safe_webhook_url("http://localhost:8000") is False
    assert is_safe_webhook_url("http://127.0.0.1:8000") is False


@pytest.mark.asyncio
async def test_webhook_notification_dispatch():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="WebhookTenant", is_active=True, balance=Decimal("100.00"))
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="SUCCESS",
            callback_url="https://example.com/webhook",
        )
        db.add(tenant)
        db.add(task)
        await db.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("app.services.webhook_service.is_safe_webhook_url", return_value=True), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            await send_webhook_notification(
                db=db,
                task_id=task_id,
                callback_url="https://example.com/webhook",
                secret="secret_123",
                payload_dict={"data": {"shipment": {"pol": "Yantian"}}},
            )


@pytest.mark.asyncio
async def test_queue_and_extraction_pipeline():
    manager = TaskQueueManager()
    await manager.start()

    t_id = f"tenant_{uuid.uuid4().hex[:6]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="PipeTenant",
            is_active=True,
            balance=Decimal("50.00"),
            unit_price=Decimal("1.00"),
        )
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            mail_subject="Test Pipeline",
            raw_input_json=json.dumps({"mail_body": "Please book container MSCU9988776", "attachments": []}),
        )
        db.add(tenant)
        db.add(task)
        await db.commit()

    with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"POL": "PIPE_123"}
        await manager.enqueue(task_id, t_id, "secret_key")
        await asyncio.sleep(0.5)

    await manager.stop()
