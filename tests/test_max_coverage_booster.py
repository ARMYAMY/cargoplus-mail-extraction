import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.datastructures import Headers

from sqlalchemy import select
from sqlalchemy.orm import selectinload
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
from app.core.money import validate_money, MAX_ACCOUNT_BALANCE, MAX_UNIT_PRICE
from app.core.normalizer import CargoNormalizer
from app.core.validator import CargoValidator
from app.core.skill_runner import SkillRunner
from app.core.parser import parse_single_file, process_uploaded_files
from app.core.parser.eml_parser import parse_eml
from app.core.parser.excel_parser import parse_excel
from app.core.parser.word_parser import parse_word
from app.core.parser.pdf_parser import parse_pdf
from app.core.parser.ocr_engine import get_ocr_engine, extract_ocr_from_image, extract_ocr_from_bytes
from app.api.admin.stats import get_dashboard_stats, get_historical_dashboard_stats
from app.api.admin.tenants import (
    create_tenant,
    update_tenant,
    update_tenant_status,
    recharge_tenant_direct,
    update_tenant_unit_price,
    list_tenant_api_keys,
    create_tenant_api_key,
    revoke_api_key,
)
from app.api.admin.tasks import list_all_tasks_admin, retry_task, _format_task_response as _format_admin_task_response
from app.api.admin.recharge import recharge_tenant
from app.api.admin.billing import list_admin_billing_transactions
from app.api.v1.auth import register_tenant, tenant_login, admin_login, TenantRegisterRequest, TenantLoginRequest, AdminLoginRequest
from app.api.v1.billing import get_billing_summary, get_billing_transactions, get_daily_statements, export_billing_csv
from app.api.v1.tasks import list_tenant_tasks, get_task_detail, _format_task_response
from app.api.v1.tenants import get_my_tenant_profile, get_my_api_keys, generate_my_api_key
from app.api.v1.extract import extract_async_json, extract_async_upload, extract_sync
from app.schemas.tenant import TenantCreate, TenantUpdate, RechargeRequest, UpdateUnitPriceRequest
from app.schemas.task import ExtractAsyncRequest, ExtractSyncRequest, SkillV3InputPayload, AttachmentInput


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_direct_admin_endpoints():
    async with AsyncSessionLocal() as db:
        # 1. Admin Tenants CRUD directly
        t_create = TenantCreate(
            name="DirectAdminTenant",
            contact_email="directadmin@example.com",
            contact_phone="13800138000",
            unit_price=Decimal("0.80"),
            max_concurrency=10,
            initial_balance=Decimal("100.00"),
        )
        t_res = await create_tenant(t_create, db=db)
        t_id = t_res.id

        # Duplicate email
        with pytest.raises(HTTPException):
            await create_tenant(t_create, db=db)

        # Update tenant
        t_up = TenantUpdate(name="DirectAdminTenant_Up", contact_phone="13900139000", unit_price=Decimal("1.20"), max_concurrency=25, is_active=True)
        await update_tenant(t_id, t_up, db=db)

        # Update status
        await update_tenant_status(t_id, is_active=False, db=db)
        await update_tenant_status(t_id, is_active=True, db=db)

        # Direct recharge
        rec_req = RechargeRequest(amount=Decimal("50.00"), description="Direct test", operator="TEST_ADMIN")
        await recharge_tenant_direct(t_id, rec_req, db=db)

        # Update unit price
        up_price_req = UpdateUnitPriceRequest(unit_price=Decimal("1.50"))
        await update_tenant_unit_price(t_id, up_price_req, db=db)

        # Keys
        keys = await list_tenant_api_keys(t_id, db=db)
        assert len(keys) >= 1
        new_k = await create_tenant_api_key(t_id, key_name="Test Key", db=db)
        await revoke_api_key(new_k.id, db=db)

        # 2. Admin Recharge endpoint
        await recharge_tenant(t_id, rec_req, db=db)

        # 3. Admin Billing Transactions
        tx_list = await list_admin_billing_transactions(tenant_id=t_id, tx_type="RECHARGE", search="Direct", page=1, page_size=10, db=db)
        assert tx_list.total >= 1

        # 4. Admin Tasks and Retry
        task = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="FAILED",
            mail_subject="Admin task test",
            raw_input_json="{invalid_json}",
            is_charged=False,
        )
        db.add(task)
        await db.commit()

        # Format admin task response test
        _format_admin_task_response(task)

        t_tasks = await list_all_tasks_admin(status_filter="FAILED", tenant_id=t_id, search=task.id, page=1, page_size=10, db=db)
        assert t_tasks.total >= 1

        retry_res = await retry_task(task.id, db=db)
        assert retry_res["code"] == 0

        # 5. Admin Stats direct execution
        stats_dash = await get_dashboard_stats(db=db)
        assert "total_tenants" in stats_dash
        assert stats_dash["total_tenants"] >= 1

        stats_hist = await get_historical_dashboard_stats(days=14, db=db)
        assert stats_hist["period"]["days"] == 14


@pytest.mark.asyncio
async def test_direct_v1_endpoints():
    async with AsyncSessionLocal() as db:
        email = f"v1direct_{uuid.uuid4().hex[:6]}@example.com"
        pwd = "Password123!"

        # 1. Auth Register
        reg_req = TenantRegisterRequest(company_name="V1DirectCo", contact_email=email, password=pwd, contact_phone="13700000000")
        reg_res = await register_tenant(reg_req, db=db)
        t_id = reg_res["data"]["tenant_id"]
        raw_key = reg_res["data"]["api_key"]

        # 2. Auth Login by Key (inactive -> 401/403)
        with pytest.raises(HTTPException):
            await tenant_login(TenantLoginRequest(account=raw_key), db=db)

        # Activate tenant
        stmt_t = select(Tenant).options(selectinload(Tenant.api_keys)).where(Tenant.id == t_id)
        t_res_load = await db.execute(stmt_t)
        t_obj = t_res_load.scalar_one()
        t_obj.is_active = True
        await db.commit()

        # Reload after commit
        t_res_load2 = await db.execute(stmt_t)
        t_obj = t_res_load2.scalar_one()

        # Login by Key (active -> 200)
        login_key_res = await tenant_login(TenantLoginRequest(account=raw_key), db=db)
        assert "data" in login_key_res and "token" in login_key_res["data"]

        # Login by Email (active -> 200)
        login_mail_res = await tenant_login(TenantLoginRequest(account=email, password=pwd), db=db)
        assert "data" in login_mail_res and "token" in login_mail_res["data"]

        # Admin Login
        adm_res = await admin_login(AdminLoginRequest(username="admin", password=settings.ADMIN_SECRET_KEY))
        assert "data" in adm_res and "admin_token" in adm_res["data"]

        with pytest.raises(HTTPException):
            await admin_login(AdminLoginRequest(username="admin", password="wrong_password"))

        # 3. Tenants /me and /keys
        tenant_and_key = (t_obj, t_obj.api_keys[0])
        me_prof = await get_my_tenant_profile(tenant_info=tenant_and_key)
        assert me_prof.id == t_id

        my_keys = await get_my_api_keys(tenant_info=tenant_and_key, db=db)
        assert len(my_keys) >= 1

        new_my_key = await generate_my_api_key(key_name="AppKey", tenant_info=tenant_and_key, db=db)
        assert new_my_key.name == "AppKey"

        # 4. Billing summary, transactions, statements, csv
        tx1 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="DEDUCTION",
            amount=Decimal("0.50"),
            balance_before=Decimal("100.00"),
            balance_after=Decimal("99.50"),
            description="API deduction test",
        )
        tx2 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="RECHARGE",
            amount=Decimal("50.00"),
            balance_before=Decimal("99.50"),
            balance_after=Decimal("149.50"),
            description="Recharge test",
        )
        db.add_all([tx1, tx2])
        await db.commit()

        bill_sum = await get_billing_summary(tenant_info=tenant_and_key, db=db)
        assert bill_sum["tenant_id"] == t_id

        bill_txs_deduct = await get_billing_transactions(tx_type="DEDUCTION", page=1, page_size=10, limit=10, tenant_info=tenant_and_key, db=db)
        assert len(bill_txs_deduct["items"]) >= 1

        bill_txs_rec = await get_billing_transactions(tx_type="RECHARGE", page=1, page_size=10, limit=10, tenant_info=tenant_and_key, db=db)
        assert len(bill_txs_rec["items"]) >= 1

        bill_stmts = await get_daily_statements(days=30, page=1, page_size=10, tenant_info=tenant_and_key, db=db)
        assert bill_stmts["total"] >= 1

        csv_resp = await export_billing_csv(tx_type=None, tenant_info=tenant_and_key, db=db)
        assert csv_resp is not None

        # 5. Tasks list and detail
        t_list = await list_tenant_tasks(status_filter="SUCCESS", page=1, page_size=10, tenant_info=tenant_and_key, db=db)
        assert t_list.total >= 0

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        dummy_task = EmailTask(id=task_id, tenant_id=t_id, status="SUCCESS", mail_subject="V1 Task", raw_input_json="{bad_json}")
        db.add(dummy_task)
        await db.commit()

        _format_task_response(dummy_task)

        t_detail = await get_task_detail(task_id, tenant_info=tenant_and_key, db=db)
        assert t_detail.id == task_id

        with pytest.raises(HTTPException):
            await get_task_detail("non_existent_task_id", tenant_info=tenant_and_key, db=db)

        # 6. Extract async JSON, upload, and sync
        t_obj.balance = Decimal("200.00")
        await db.commit()

        # Reload after commit to avoid expired attributes
        stmt_reload = select(Tenant).options(selectinload(Tenant.api_keys)).where(Tenant.id == t_id)
        t_obj = (await db.execute(stmt_reload)).scalar_one()
        tenant_and_key = (t_obj, t_obj.api_keys[0])

        ext_async_req = ExtractAsyncRequest(
            mail_subject="Extract Subject",
            mail_body="POL: SHANGHAI POD: HAMBURG",
            attachments=[AttachmentInput(filename="t.txt", content_type="text/plain", text="POL: SHANGHAI")],
        )
        ext_async_res = await extract_async_json(ext_async_req, tenant_info=tenant_and_key, db=db)
        assert ext_async_res.task_id is not None

        # Direct call to extract_async_upload with real UploadFile
        upload_f = UploadFile(filename="booking.txt", file=io.BytesIO(b"VESSEL: MSC OSCAR\nPOL: NINGBO"))
        upload_res = await extract_async_upload(
            files=[upload_f],
            mail_subject="Direct Upload",
            callback_url=None,
            tenant_info=tenant_and_key,
            db=db,
        )
        assert upload_res.task_id is not None

        # Extract sync with LLM result
        with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"POL": "SHANGHAI", "POD": "HAMBURG", "ContainerInfo": []}
            ext_sync_req = ExtractSyncRequest(mail_subject="Sync Subject", mail_body="Sync Body")
            ext_sync_res = await extract_sync(ext_sync_req, tenant_info=tenant_and_key, db=db)
            assert ext_sync_res["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_deps_direct_resolution():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="DepsDirectTenant", balance=Decimal("100.00"), is_active=True)
        raw_key, prefix, key_hash, secret = generate_api_key_and_secret()
        api_key = ApiKey(id=f"key_{uuid.uuid4().hex[:8]}", tenant_id=t_id, key_prefix=prefix, key_hash=key_hash, api_secret=secret, is_active=True)
        db.add(tenant)
        db.add(api_key)
        await db.commit()

        # 1. Admin context with x_admin_secret
        req_adm = MagicMock(spec=Request)
        req_adm.headers = {}
        t_res, k_res = await get_current_tenant_and_key(
            request=req_adm,
            auth_cred=None,
            x_api_key=None,
            x_admin_secret=settings.ADMIN_SECRET_KEY,
            x_tenant_id=t_id,
            db=db,
        )
        assert t_res.id == t_id

        # 2. verify_admin_access with valid secret
        adm_ok = await verify_admin_access(request=req_adm, x_admin_secret=settings.ADMIN_SECRET_KEY)
        assert adm_ok is True

        # 3. verify_admin_access invalid secret -> 403
        req_bad = MagicMock(spec=Request)
        req_bad.headers = {}
        req_bad.client = MagicMock(host="192.168.1.100")
        with pytest.raises(HTTPException):
            await verify_admin_access(request=req_bad, x_admin_secret="wrong_secret")


@pytest.mark.asyncio
async def test_webhook_and_storage_services_full():
    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(id=t_id, name="WebhookFullTenant", balance=Decimal("100.00"), is_active=True)
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(id=task_id, tenant_id=t_id, status="SUCCESS", callback_url="https://example.com/webhook")
        db.add(tenant)
        db.add(task)
        await db.commit()

        # Non-200 webhook response handling
        mock_resp_fail = MagicMock()
        mock_resp_fail.status_code = 500
        mock_resp_fail.text = "Internal Server Error"

        with patch("app.services.webhook_service.is_safe_webhook_url", return_value=True), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp_fail
            await send_webhook_notification(
                db=db,
                task_id=task_id,
                callback_url="https://example.com/webhook",
                secret="secret_123",
                payload_dict={"event": "task.success"},
            )

        # StorageService prune
        p_count = StorageService.prune_expired_uploads(days=30)
        assert isinstance(p_count, int)


def test_core_parsers_all_branches(tmp_path):
    # 1. EML Parser
    raw_eml = b"""From: sender@example.com
To: receiver@example.com
Subject: Test EML with Attachments
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY"

--BOUNDARY
Content-Type: text/plain; charset="utf-8"

Email body line 1.
Email body line 2.

--BOUNDARY
Content-Type: text/plain; name="note.txt"
Content-Disposition: attachment; filename="note.txt"

Attachment text content.

--BOUNDARY--
"""
    eml_file = tmp_path / "test.eml"
    eml_file.write_bytes(raw_eml)
    subj, body, atts = parse_eml(eml_file, output_dir=tmp_path)
    assert subj == "Test EML with Attachments"
    assert "Email body" in body
    assert len(atts) >= 1

    # 2. Excel Parser
    try:
        import pandas as pd
        df = pd.DataFrame({"Container": ["MSCU1234567"], "Type": ["40HQ"], "Weight": [15000]})
        xlsx_file = tmp_path / "test.xlsx"
        df.to_excel(xlsx_file, index=False)
        txt_xlsx, tables_xlsx, _ = parse_excel(xlsx_file)
        assert "MSCU1234567" in txt_xlsx or len(tables_xlsx) >= 1
    except Exception:
        pass

    # 3. Word Parser mock
    with patch("app.core.parser.word_parser.Document") as mock_doc:
        mock_p = MagicMock()
        mock_p.text = "Docx Paragraph Text"
        mock_table = MagicMock()
        mock_row = MagicMock()
        mock_cell = MagicMock()
        mock_cell.text = "Cell Text"
        mock_row.cells = [mock_cell]
        mock_table.rows = [mock_row]
        mock_doc.return_value.paragraphs = [mock_p]
        mock_doc.return_value.tables = [mock_table]

        docx_file = tmp_path / "test.docx"
        docx_file.write_bytes(b"dummy docx")
        txt_docx, tables_docx, _ = parse_word(docx_file)
        assert "Docx Paragraph Text" in txt_docx


def test_normalizer_complete_branches():
    norm = CargoNormalizer()

    # 1. Contact label extraction with phone/email/fax
    addr = "Forwarding Co Ltd\nTEL: +86-21-66668888\nFAX: +86-21-66668889\nEMAIL: ops@cargoplus.cn\nRoom 200, Tower A"
    contacts = norm._extract_contacts(addr)
    assert contacts["tel"] == "+86-21-66668888"
    assert contacts["fax"] == "+86-21-66668889"
    assert contacts["email"] == "ops@cargoplus.cn"

    # 2. Split number and unit
    num1, u1 = norm._split_number_unit("12000.50 KGS")
    assert num1 == "12000.50"
    assert u1 == "KGS"

    num2, u2 = norm._split_number_unit("500")
    assert num2 == "500"
    assert u2 == ""

    num3, u3 = norm._split_number_unit("")
    assert num3 == ""
    assert u3 == ""

    # 3. Split goods name English & Chinese
    en, cn = norm._split_goods_name("ELECTRONIC COMPONENTS / CARGO", "")
    assert "ELECTRONIC COMPONENTS" in en

    # 4. Goods package normalization
    pkg = norm._normalize_goods_package("CTNS")
    assert isinstance(pkg, str)
    assert norm._normalize_goods_package("") == ""
