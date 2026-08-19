import io
import json
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.tenant import Tenant, ApiKey
from app.models.task import EmailTask
from app.models.billing import BillingTransaction
from app.core.parser import parse_single_file, process_uploaded_files
from app.services.storage_service import StorageService
from app.services.extraction_service import ExtractionService
from app.config import settings


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


@pytest.mark.asyncio
async def test_admin_stats_today_and_history_branches():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="StatsTodayTenant",
            contact_email=f"{t_id}@example.com",
            is_active=True,
            balance=Decimal("500.00"),
            unit_price=Decimal("1.00"),
        )
        # Create tasks today
        now = datetime.now(timezone.utc)
        task1 = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="SUCCESS",
            duration_ms=500,
            created_at=now,
            charged_amount=Decimal("1.00"),
            is_charged=True,
        )
        task2 = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="FAILED",
            duration_ms=300,
            created_at=now,
        )
        tx1 = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            type="DEDUCTION",
            amount=Decimal("1.00"),
            balance_before=Decimal("500.00"),
            balance_after=Decimal("499.00"),
            created_at=now,
        )
        db.add_all([tenant, task1, task2, tx1])
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /admin/stats (hitting lines 32-112)
        res = await client.get("/admin/stats", headers=admin_headers)
        assert res.status_code == 200
        stats = res.json()
        assert stats["today_total"] >= 2
        assert stats["today_success"] >= 1
        assert len(stats["history_14_days"]) == 14

        # GET /admin/stats/history (hitting lines 172-345)
        res_hist = await client.get("/admin/stats/history?days=30", headers=admin_headers)
        assert res_hist.status_code == 200
        assert res_hist.json()["period"]["days"] == 30


@pytest.mark.asyncio
async def test_admin_tasks_and_billing_branches():
    admin_headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}
    transport = ASGITransport(app=app)
    t_id = f"tenant_{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=t_id,
            name="AdminTasksTenant",
            contact_email=f"{t_id}@example.com",
            is_active=True,
            balance=Decimal("200.00"),
            unit_price=Decimal("1.00"),
        )
        task_charged = EmailTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            status="FAILED",
            is_charged=True,  # Already charged -> cannot retry (409)
            mail_subject="Charged Task",
        )
        tx = BillingTransaction(
            id=f"tx_{uuid.uuid4().hex[:8]}",
            tenant_id=t_id,
            task_id=task_charged.id,
            type="DEDUCTION",
            amount=Decimal("1.00"),
            balance_before=Decimal("200.00"),
            balance_after=Decimal("199.00"),
            description="Searchable Description 123",
        )
        db.add_all([tenant, task_charged, tx])
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Retry already charged task -> 409 Conflict
        res_retry_conflict = await client.post(f"/admin/tasks/{task_charged.id}/retry", headers=admin_headers)
        assert res_retry_conflict.status_code == 409

        # 2. Query billing transactions with search & type & tenant_id
        res_tx = await client.get(
            f"/admin/billing/transactions?tenant_id={t_id}&type=DEDUCTION&search=Searchable",
            headers=admin_headers,
        )
        assert res_tx.status_code == 200
        assert res_tx.json()["total"] >= 1


def test_core_parsers_dispatcher_and_file_types(tmp_path):
    # 1. Text & JSON & CSV & Markdown files
    f_txt = tmp_path / "doc.txt"
    f_txt.write_text("Hello txt", encoding="utf-8")
    att_txt = parse_single_file(f_txt)
    assert att_txt.content_type == "text/plain"

    f_csv = tmp_path / "doc.csv"
    f_csv.write_text("a,b\n1,2", encoding="utf-8")
    att_csv = parse_single_file(f_csv)
    assert att_csv.content_type == "text/plain"

    f_json = tmp_path / "doc.json"
    f_json.write_text('{"key": "val"}', encoding="utf-8")
    att_json = parse_single_file(f_json)
    assert att_json.content_type == "text/plain"

    f_md = tmp_path / "doc.md"
    f_md.write_text("# Markdown", encoding="utf-8")
    att_md = parse_single_file(f_md)
    assert att_md.content_type == "text/plain"

    # 2. Unsupported extension
    f_bin = tmp_path / "doc.unknown"
    f_bin.write_bytes(b"\x00\x01\x02")
    att_bin = parse_single_file(f_bin)
    assert "Binary or unsupported" in att_bin.text

    # 3. Image extension
    f_png = tmp_path / "doc.png"
    f_png.write_bytes(b"\x89PNG")
    att_png = parse_single_file(f_png)
    assert att_png.content_type == "image/png"

    # 4. Process uploaded files with EML mock
    f_eml = tmp_path / "mail.eml"
    f_eml.write_bytes(b"From: test@example.com\nSubject: EML Test\n\nBody of email")
    payload = process_uploaded_files(
        file_paths=[f_eml, f_txt],
        subject="Consolidated Subject",
        body="Consolidated Body",
        temp_dir=tmp_path,
    )
    assert payload.mail_subject == "Consolidated Subject"
    assert len(payload.attachments) >= 1


@pytest.mark.asyncio
async def test_extraction_service_file_attachments():
    # Save file inside settings.uploads_path to satisfy path containment check
    f_txt = settings.uploads_path / f"test_booking_{uuid.uuid4().hex[:8]}.txt"
    f_txt.write_text("POL: NINGBO POD: ROTTERDAM CONTAINER: CCLU9988776", encoding="utf-8")

    async with AsyncSessionLocal() as db:
        t_id = f"tenant_{uuid.uuid4().hex[:6]}"
        tenant = Tenant(
            id=t_id,
            name="FileTaskTenant",
            contact_email=f"{t_id}@example.com",
            is_active=True,
            balance=Decimal("100.00"),
            unit_price=Decimal("1.00"),
        )
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            input_type="FILE",
            mail_subject="File Processing Task",
            file_paths=json.dumps([str(f_txt)]),
            status="PENDING",
            is_reserved=True,
            reserved_amount=Decimal("1.00"),
            callback_url="https://example.com/callback",
        )
        tenant.reserved_balance = Decimal("1.00")
        db.add(tenant)
        db.add(task)
        await db.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "OK"

    with patch("app.core.skill_runner.SkillRunner.extract_draft_json", new_callable=AsyncMock) as mock_llm, \
         patch("app.services.webhook_service.is_safe_webhook_url", return_value=True), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_wh:
        mock_llm.return_value = {"POL": "NINGBO", "POD": "ROTTERDAM", "ContainerInfo": []}
        mock_wh.return_value = mock_resp

        await ExtractionService.process_task(task_id, tenant_secret="secret_key")

    async with AsyncSessionLocal() as check_db:
        t_res = await check_db.get(EmailTask, task_id)
        assert t_res.status == "SUCCESS"
        assert t_res.callback_status == "SUCCESS"
