import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.services.auth_service import generate_api_key_and_secret, hash_password, password_needs_rehash
from app.api.v1.auth import register_tenant, tenant_login, admin_login, TenantRegisterRequest, TenantLoginRequest, AdminLoginRequest
from app.api.v1.extract import (
    extract_async_json,
    extract_async_upload,
    enforce_queue_capacity,
    normalize_idempotency_key,
    utc_now,
    ExtractAsyncRequest,
)


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_auth_all_remaining_branches():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        email = f"auth_rem_{t_id}@example.com"
        pwd = "TestPassword123!"
        tenant = Tenant(
            id=t_id,
            name="AuthRemTenant",
            contact_email=email,
            password_hash=hash_password(pwd),
            balance=Decimal("100.00"),
            is_active=False,  # Inactive
        )
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(
            id=f"key_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            name="test-key",
            key_prefix=prefix,
            key_hash=key_hash,
            api_secret=secret,
            is_active=True,
        )
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. register_tenant with already registered email -> 400
        with pytest.raises(HTTPException) as exc_reg_dup:
            await register_tenant(TenantRegisterRequest(company_name="Dup", contact_email=email, password="Password123"), db=db)
        assert exc_reg_dup.value.status_code == 400

        # 2. Login by key with inactive tenant -> 403
        with pytest.raises(HTTPException) as exc_k_inact:
            await tenant_login(TenantLoginRequest(account=raw_key), db=db)
        assert exc_k_inact.value.status_code == 403

        # 3. Login by key prefix where tenant is inactive -> 403
        with pytest.raises(HTTPException) as exc_pref_inact:
            await tenant_login(TenantLoginRequest(account=f"{prefix}.invalid_hash_str"), db=db)
        assert exc_pref_inact.value.status_code == 403

        # 4. Login by email with password needing rehash (active tenant)
        tenant.is_active = True
        # Set legacy sha256 hash
        import hashlib
        tenant.password_hash = hashlib.sha256(f"cargo_pwd_salt_{pwd}".encode("utf-8")).hexdigest()
        await db.commit()

        login_rehash = await tenant_login(TenantLoginRequest(account=email, password=pwd), db=db)
        assert login_rehash["code"] == 0

        # Check tenant password was rehashed
        await db.refresh(tenant)
        assert tenant.password_hash.startswith("pbkdf2_sha256$")

        # 5. admin_login with wrong username -> 401
        with pytest.raises(HTTPException) as exc_adm_usr:
            await admin_login(AdminLoginRequest(username="not_admin", password=settings.ADMIN_SECRET_KEY))
        assert exc_adm_usr.value.status_code == 401


@pytest.mark.asyncio
async def test_extract_capacity_and_helpers():
    # 1. utc_now helper
    now_dt = utc_now()
    assert now_dt.tzinfo is not None

    # 2. normalize_idempotency_key with unprintable characters -> 422
    with pytest.raises(HTTPException):
        normalize_idempotency_key("bad\x00key")

    with pytest.raises(HTTPException):
        normalize_idempotency_key("a" * 200)

    # 3. enforce_queue_capacity global pending limit -> 429
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:8]}"
        with patch.object(settings, "MAX_GLOBAL_PENDING_TASKS", 0):
            with pytest.raises(HTTPException) as exc_glob_429:
                await enforce_queue_capacity(db, t_id)
            assert exc_glob_429.value.status_code == 429
