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

from app.config import settings
from app.database import AsyncSessionLocal, init_db, get_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask, WebhookLog
from app.models.billing import BillingTransaction
from app.main import seed_initial_demo_tenant, lifespan, app
from app.api.admin.tenants import (
    create_tenant,
    update_tenant,
    update_tenant_status,
    recharge_tenant_direct,
    update_tenant_unit_price,
    create_tenant_api_key,
    revoke_api_key,
)
from app.core.parser.excel_parser import parse_excel
from app.core.parser.pdf_parser import parse_pdf
from app.core.parser.word_parser import parse_word
from app.core.parser import parse_single_file, process_uploaded_files
from app.schemas.tenant import TenantCreate, TenantUpdate, RechargeRequest, UpdateUnitPriceRequest
from app.schemas.task import SkillV3InputPayload


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_main_lifespan_and_demo_seed():
    # 1. seed_initial_demo_tenant when no tenants exist
    with patch("app.database.AsyncSessionLocal") as mock_session:
        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db
        # First query returns None (no existing tenant)
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = mock_res
        mock_session.return_value = mock_db

        await seed_initial_demo_tenant()

    # 2. lifespan context manager
    with patch("app.services.queue_service.task_queue.start", new_callable=AsyncMock), \
         patch("app.services.queue_service.task_queue.stop", new_callable=AsyncMock), \
         patch("app.services.storage_service.StorageService.start_retention_pruning_worker", new_callable=AsyncMock):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_admin_tenants_not_found_branches():
    async with AsyncSessionLocal() as db:
        fake_id = "non_existent_tenant_9999"

        # 1. update_tenant 404
        with pytest.raises(HTTPException) as exc1:
            await update_tenant(fake_id, TenantUpdate(name="New"), db=db)
        assert exc1.value.status_code == 404

        # 2. update_tenant_status 404
        with pytest.raises(HTTPException) as exc2:
            await update_tenant_status(fake_id, is_active=True, db=db)
        assert exc2.value.status_code == 404

        # 3. recharge_tenant_direct 404
        with pytest.raises(HTTPException) as exc3:
            await recharge_tenant_direct(fake_id, RechargeRequest(amount=Decimal("10.00")), db=db)
        assert exc3.value.status_code == 404

        # 4. update_tenant_unit_price 404
        with pytest.raises(HTTPException) as exc4:
            await update_tenant_unit_price(fake_id, UpdateUnitPriceRequest(unit_price=Decimal("1.00")), db=db)
        assert exc4.value.status_code == 404

        # 5. create_tenant_api_key 404
        with pytest.raises(HTTPException) as exc5:
            await create_tenant_api_key(fake_id, key_name="Key", db=db)
        assert exc5.value.status_code == 404

        # 6. revoke_api_key 404
        with pytest.raises(HTTPException) as exc6:
            await revoke_api_key("non_existent_key_9999", db=db)
        assert exc6.value.status_code == 404


def test_excel_parser_all_branches(tmp_path):
    # 1. Corrupt excel file
    bad_xlsx = tmp_path / "bad.xlsx"
    bad_xlsx.write_bytes(b"corrupt excel content")
    txt_err, tbls_err, ocr_err = parse_excel(bad_xlsx)
    assert "Error parsing Excel" in ocr_err

    # 2. Excel with large rows (> 100 rows)
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "LargeSheet"
    ws.append(["Col1", "Col2"])
    for i in range(120):
        ws.append([f"Val1_{i}", f"Val2_{i}"])

    # Add empty sheet
    wb.create_sheet(title="EmptySheet")

    large_xlsx = tmp_path / "large.xlsx"
    wb.save(large_xlsx)

    txt_ok, tbls_ok, _ = parse_excel(large_xlsx)
    assert "LargeSheet" in txt_ok
    assert len(tbls_ok) == 1


def test_pdf_and_word_parser_branches(tmp_path):
    # 1. PDF with parsing exception
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"corrupt pdf")
    txt_p_err, tbl_p_err, ocr_p_err = parse_pdf(bad_pdf)
    assert "Error parsing PDF" in ocr_p_err

    # 2. Word with parsing exception
    bad_docx = tmp_path / "bad.docx"
    bad_docx.write_bytes(b"corrupt docx")
    txt_w_err, tbl_w_err, ocr_w_err = parse_word(bad_docx)
    assert "Error parsing Word" in ocr_w_err


def test_parser_dispatcher_all_file_types(tmp_path):
    # 1. .txt file
    f_txt = tmp_path / "doc.txt"
    f_txt.write_text("POL: SHANGHAI POD: ROTTERDAM", encoding="utf-8")
    att_txt = parse_single_file(f_txt)
    assert att_txt.content_type == "text/plain"
    assert "POL: SHANGHAI" in att_txt.text

    # 2. Unknown extension
    f_unk = tmp_path / "binary.bin"
    f_unk.write_bytes(b"\x00\x01\x02")
    att_unk = parse_single_file(f_unk)
    assert "Binary or unsupported" in att_unk.text

    # 3. Exception in parse_single_file
    with patch("app.core.parser.parse_pdf", side_effect=RuntimeError("Fatal PDF Engine Crash")):
        f_pdf = tmp_path / "crash.pdf"
        f_pdf.write_bytes(b"%PDF-1.4")
        att_crash = parse_single_file(f_pdf)
        assert "Error parsing attachment" in att_crash.text

    # 4. process_uploaded_files with .eml without initial subject
    raw_eml = b"""From: forwarder@company.com
To: user@cargoplus.cn
Subject: Auto Extracted Subject EML
Content-Type: text/plain; charset="utf-8"

Body text in EML.
"""
    eml_file = tmp_path / "auto.eml"
    eml_file.write_bytes(raw_eml)

    payload = process_uploaded_files(file_paths=[eml_file], subject="", body="", temp_dir=tmp_path)
    assert payload.mail_subject == "Auto Extracted Subject EML"
    assert "Body text in EML" in payload.mail_body
