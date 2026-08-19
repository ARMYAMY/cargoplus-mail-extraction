import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.auth_service import generate_api_key_and_secret, create_access_token
from app.services.billing_service import BillingService
from app.api.deps import get_current_tenant_and_key, verify_admin_access


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_billing_service_remaining_lines():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        invalid_tenant = Tenant(
            id=t_id,
            name="BillRemainingTenant",
            balance=Decimal("-10.00"),  # Negative balance -> invalid state
            reserved_balance=Decimal("0.00"),
            unit_price=Decimal("0.50"),
            is_active=True,
        )
        db.add(invalid_tenant)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        tenant = Tenant(
            id=t_id,
            name="BillRemainingTenant",
            balance=Decimal("100.00"),
            reserved_balance=Decimal("0.50"),
            unit_price=Decimal("0.50"),
            is_active=True,
        )
        db.add(tenant)
        await db.commit()

        # 2. release_task_reservation with invalid task.reserved_amount (< MIN_UNIT_PRICE) -> ValueError
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task_bad_res = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="PENDING",
            is_reserved=True,
            reserved_amount=Decimal("0.001"),  # Invalid < 0.01
        )
        db.add(task_bad_res)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        # 4. deduct_for_task_success with task already charged -> lines 241-242
        task_id2 = f"task_{uuid.uuid4().hex[:8]}"
        task_already_charged = EmailTask(
            id=task_id2,
            tenant_id=t_id,
            status="SUCCESS",
            is_charged=True,  # Already charged!
            is_reserved=True,
            reserved_amount=Decimal("0.50"),
        )
        db.add(task_already_charged)
        await db.commit()

        res_already = await BillingService.deduct_for_task_success(db, t_id, task_id2)
        assert res_already is None


@pytest.mark.asyncio
async def test_deps_auth_branches():
    t_id = f"tenant_{uuid.uuid4().hex[:8]}"
    raw_key, prefix, key_hash, secret = generate_api_key_and_secret()

    async with AsyncSessionLocal() as db:
        tenant = Tenant(id=t_id, name="DepsTenant", balance=Decimal("100.00"), is_active=True)
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, name="k", key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. get_current_tenant_and_key with valid session token
        request = MagicMock(spec=Request)
        sess_token = create_access_token(t_id, role="tenant")
        session_cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=sess_token)
        t_out, k_out = await get_current_tenant_and_key(
            request=request,
            auth_cred=session_cred,
            x_api_key=None,
            x_admin_secret=None,
            x_tenant_id=None,
            db=db,
        )
        assert t_out.id == t_id
        assert k_out.id == api_key.id

        # 2. get_current_tenant_and_key with raw API key
        api_cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_key)
        t_out2, k_out2 = await get_current_tenant_and_key(
            request=request,
            auth_cred=api_cred,
            x_api_key=None,
            x_admin_secret=None,
            x_tenant_id=None,
            db=db,
        )
        assert t_out2.id == t_id
        assert k_out2.id == api_key.id

    # 3. verify_admin_access with DEBUG=True on localhost
    mock_request = MagicMock()
    mock_request.headers.get.return_value = ""
    mock_request.client.host = "127.0.0.1"

    with patch.object(settings, "DEBUG", True):
        is_ok = await verify_admin_access(mock_request, x_admin_secret=None)
        assert is_ok is True
