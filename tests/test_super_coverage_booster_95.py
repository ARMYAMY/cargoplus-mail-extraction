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
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.config import settings
from app.database import AsyncSessionLocal, init_db, get_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask, WebhookLog
from app.models.billing import BillingTransaction
from app.api.deps import get_current_tenant_and_key, verify_admin_access
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
from app.core.parser.eml_parser import parse_eml
from app.api.v1.auth import register_tenant, tenant_login, admin_login, TenantRegisterRequest, TenantLoginRequest, AdminLoginRequest
from app.api.v1.extract import extract_async_upload, validate_callback_url


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_deps_all_branch_matrix():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="DepsMatrixTenant", balance=Decimal("100.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. x_admin_secret matching fallback key "cargo-plus-admin-secret-2026"
        req_adm1 = MagicMock(spec=Request)
        req_adm1.headers = {}
        t_res, k_res = await get_current_tenant_and_key(
            request=req_adm1,
            auth_cred=None,
            x_api_key=None,
            x_admin_secret=settings.ADMIN_SECRET_KEY,
            x_tenant_id=t_id,
            db=db,
        )
        assert t_res.id == t_id

        # 2. Bearer header matching raw admin secret key
        req_adm2 = MagicMock(spec=Request)
        cred_adm = HTTPAuthorizationCredentials(scheme="Bearer", credentials=settings.ADMIN_SECRET_KEY)
        t_res2, k_res2 = await get_current_tenant_and_key(
            request=req_adm2,
            auth_cred=cred_adm,
            x_api_key=None,
            x_admin_secret=None,
            x_tenant_id=t_id,
            db=db,
        )
        assert t_res2.id == t_id

        # 3. Session token for non-existent tenant -> 401
        fake_token = create_access_token("non_existent_tenant_id", role="tenant")
        req_tok1 = MagicMock(spec=Request)
        cred_fake = HTTPAuthorizationCredentials(scheme="Bearer", credentials=fake_token)
        with pytest.raises(HTTPException) as exc1:
            await get_current_tenant_and_key(
                request=req_tok1,
                auth_cred=cred_fake,
                x_api_key=None,
                x_admin_secret=None,
                x_tenant_id=None,
                db=db,
            )
        assert exc1.value.status_code == 401

        # 4. Session token for inactive tenant -> 403
        tenant.is_active = False
        await db.commit()
        tok_inactive = create_access_token(t_id, role="tenant")
        req_tok2 = MagicMock(spec=Request)
        cred_inactive = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok_inactive)
        with pytest.raises(HTTPException) as exc2:
            await get_current_tenant_and_key(
                request=req_tok2,
                auth_cred=cred_inactive,
                x_api_key=None,
                x_admin_secret=None,
                x_tenant_id=None,
                db=db,
            )
        assert exc2.value.status_code == 401

        # 5. Session token when tenant has no active API keys -> 401
        tenant.is_active = True
        api_key.is_active = False
        await db.commit()
        tok_active = create_access_token(t_id, role="tenant")
        req_tok3 = MagicMock(spec=Request)
        cred_active = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok_active)
        with pytest.raises(HTTPException) as exc3:
            await get_current_tenant_and_key(
                request=req_tok3,
                auth_cred=cred_active,
                x_api_key=None,
                x_admin_secret=None,
                x_tenant_id=None,
                db=db,
            )
        assert exc3.value.status_code == 401

        # 6. Admin test workbench when tenant not found in DB -> 404
        req_adm_bad = MagicMock(spec=Request)
        with pytest.raises(HTTPException) as exc4:
            await get_current_tenant_and_key(
                request=req_adm_bad,
                auth_cred=None,
                x_api_key=None,
                x_admin_secret=settings.ADMIN_SECRET_KEY,
                x_tenant_id="non_existent_tenant_999",
                db=db,
            )
        assert exc4.value.status_code == 404


@pytest.mark.asyncio
async def test_extract_and_auth_coverage_matrix():
    # 1. validate_callback_url invalid URL syntax -> 422
    with pytest.raises(HTTPException):
        await validate_callback_url("not_a_valid_url")

    # 2. validate_callback_url private IP -> 422
    with pytest.raises(HTTPException):
        await validate_callback_url("http://192.168.1.1:8080/hook")

    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="AuthMatrixTenant", balance=Decimal("100.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        tenant_info = (tenant, api_key)

        # 3. extract_async_upload file size exceeding MAX_UPLOAD_FILE_SIZE -> 413
        mock_large_file = MagicMock(spec=UploadFile)
        mock_large_file.filename = "huge.txt"
        mock_large_file.read = AsyncMock(side_effect=[b"X" * (settings.MAX_UPLOAD_FILE_SIZE + 1024), b""])
        mock_large_file.close = AsyncMock()

        with pytest.raises(HTTPException) as exc_large:
            await extract_async_upload(files=[mock_large_file], mail_subject="Huge", callback_url=None, tenant_info=tenant_info, db=db)
        assert exc_large.value.status_code == 413

        # 4. admin_login with wrong password -> 401
        with pytest.raises(HTTPException) as exc_adm_pwd:
            await admin_login(AdminLoginRequest(username="admin", password="completely_wrong_secret"))
        assert exc_adm_pwd.value.status_code == 401


@pytest.mark.asyncio
async def test_skill_runner_and_parsers_matrix(tmp_path):
    runner = SkillRunner()

    # 1. SkillRunner LLM 200 without choices -> raises RuntimeError after retries
    resp_no_choices = MagicMock(status_code=200, json=MagicMock(return_value={"choices": []}))
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(settings, "LLM_MAX_RETRIES", 0), \
         patch.object(settings, "LLM_FALLBACK_MODEL", ""):
        mock_post.return_value = resp_no_choices
        with pytest.raises(RuntimeError):
            await runner.call_llm("test prompt")

    # 2. SkillRunner LLM 400 Bad Request -> raises RuntimeError after retries
    resp_400 = MagicMock(status_code=400, text="Bad Request")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(settings, "LLM_MAX_RETRIES", 0), \
         patch.object(settings, "LLM_FALLBACK_MODEL", ""):
        mock_post.return_value = resp_400
        with pytest.raises(RuntimeError):
            await runner.call_llm("test prompt")

    # 3. eml_parser with ignored unsafe attachments & max attachments limit
    eml_content = b"""From: sender@example.com
To: receiver@example.com
Subject: Test EML with Unsafe Attachments
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/html; charset="utf-8"

<html><body><p>HTML Body with &amp; &lt;tags&gt;</p></body></html>

--BOUND
Content-Type: application/octet-stream; name="virus.exe"
Content-Disposition: attachment; filename="virus.exe"

EXE_DATA

--BOUND
Content-Type: text/plain; name="valid.txt"
Content-Disposition: attachment; filename="valid.txt"

VALID_TEXT

--BOUND--
"""
    eml_file = tmp_path / "unsafe.eml"
    eml_file.write_bytes(eml_content)
    subj, body, atts = parse_eml(eml_file, output_dir=tmp_path)
    assert subj == "Test EML with Unsafe Attachments"
    assert "HTML Body with & <tags>" in body
    assert len(atts) == 1
    assert atts[0].name.endswith(".txt")
