import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.api.v1.auth import register_tenant, tenant_login, admin_login, TenantRegisterRequest, TenantLoginRequest, AdminLoginRequest
from app.celery_tasks import _deliver_task_webhook


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_auth_all_missing_branches():
    async with AsyncSessionLocal() as db:
        # 1. register_tenant commit IntegrityError -> 409 (lines 82-84)
        with patch.object(db, "commit", side_effect=IntegrityError("stmt", "params", Exception("dup"))):
            with pytest.raises(HTTPException) as exc_reg_409:
                await register_tenant(
                    TenantRegisterRequest(
                        company_name="Dup Corp",
                        contact_email="dup_corp@example.com",
                        password="Password123!",
                    ),
                    db=db,
                )
            assert exc_reg_409.value.status_code == 409

        # 2. tenant_login when multiple matching tenants exist -> 409 (lines 185-188)
        dup_email = f"dup_{uuid.uuid4().hex[:6]}@example.com"
        t1 = Tenant(id=f"t1_{uuid.uuid4().hex[:6]}", name="Dup1", contact_email=dup_email, is_active=True)
        t2 = Tenant(id=f"t2_{uuid.uuid4().hex[:6]}", name="Dup2", contact_email=dup_email, is_active=True)
        
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [t1, t2]

        with patch.object(db, "execute", return_value=mock_result):
            with pytest.raises(HTTPException) as exc_login_409:
                await tenant_login(
                    TenantLoginRequest(account=dup_email, password="Password123!"),
                    db=db,
                )
            assert exc_login_409.value.status_code == 409

    # 3. admin_login hmac comparison exception fallback (lines 243-246)
    with patch("hmac.compare_digest", side_effect=Exception("Encoding error")):
        res_admin = await admin_login(
            AdminLoginRequest(username="admin", password=settings.ADMIN_SECRET_KEY)
        )
        assert res_admin["code"] == 0
        assert "admin_token" in res_admin["data"]


@pytest.mark.asyncio
async def test_celery_deliver_webhook_missing_key_and_failed_task():
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="NoKeyTenant", balance=Decimal("100.00"), is_active=True)
        # Task with status FAILED and no active API key
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="FAILED",
            error_message="Extraction timed out",
            callback_url="https://api.example.com/webhook",
        )
        db.add(tenant)
        db.add(task)
        await db.commit()

    # Deliver webhook when tenant has no API key -> sets callback_status="FAILED"
    await _deliver_task_webhook(task_id)

    async with AsyncSessionLocal() as db:
        t_check = await db.get(EmailTask, task_id)
        assert t_check.callback_status == "FAILED"
