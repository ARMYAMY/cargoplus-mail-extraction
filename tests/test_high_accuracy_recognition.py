from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfWriter

import app.main  # Initialize the application import graph used by production startup.
from app.core.parser.pdf_parser import parse_pdf
from app.schemas.task import TaskAsyncResponse, TaskDetailResponse
from app.services.vision_service import HighAccuracyVisionError, VisionBudget, VisionService


def _blank_pdf(path: Path, pages: int = 2) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as output:
        writer.write(output)


def test_high_accuracy_pdf_renders_and_transcribes_every_page(tmp_path):
    pdf_path = tmp_path / "two-pages.pdf"
    _blank_pdf(pdf_path)
    report = {"pages_total": 0, "pages_processed": 0, "duration_ms": 0}

    def fake_transcribe(image_bytes, **kwargs):
        assert image_bytes.startswith(b"\x89PNG")
        assert kwargs["allow_local_fallback"] is False
        assert kwargs["enabled"] is True
        return "POL: NINGBO\nPOD: JAKARTA"

    with patch.object(VisionService, "transcribe_image_sync", side_effect=fake_transcribe) as mocked:
        text, tables, ocr_text = parse_pdf(
            pdf_path,
            VisionBudget(max_attempts=2),
            recognition_mode="high_accuracy",
            vision_report=report,
        )

    assert text == ""
    assert tables == []
    assert mocked.call_count == 2
    assert report["pages_total"] == 2
    assert report["pages_processed"] == 2
    assert "PDF第1页 高精度视觉识别内容" in ocr_text
    assert "PDF第2页 高精度视觉识别内容" in ocr_text


def test_standard_pdf_does_not_force_page_vision(tmp_path):
    pdf_path = tmp_path / "standard.pdf"
    _blank_pdf(pdf_path, pages=1)

    with patch.object(VisionService, "transcribe_image_sync") as mocked:
        parse_pdf(pdf_path, VisionBudget(max_attempts=1), recognition_mode="standard")

    mocked.assert_not_called()


def test_high_accuracy_does_not_silently_fallback_to_local_ocr():
    with patch.object(VisionService, "optimize_image_for_vision", return_value=b"image"):
        with pytest.raises(HighAccuracyVisionError, match="不可用"):
            VisionService.transcribe_image_sync(
                b"image",
                enabled=False,
                allow_local_fallback=False,
            )


def test_task_responses_expose_recognition_metadata():
    from datetime import datetime, timezone
    from decimal import Decimal

    submitted = TaskAsyncResponse(
        task_id="task_1",
        status="PENDING",
        created_at=datetime.now(timezone.utc),
        recognition_mode="high_accuracy",
    )
    assert submitted.recognition_mode == "high_accuracy"

    detail = TaskDetailResponse(
        id="task_1",
        tenant_id="tenant_1",
        input_type="FILE",
        mail_subject="sample.pdf",
        status="SUCCESS",
        input_summary=None,
        error_message=None,
        charged_amount=Decimal("0.5"),
        is_charged=True,
        reserved_amount=Decimal("0"),
        is_reserved=False,
        duration_ms=123,
        callback_url=None,
        callback_status="NONE",
        created_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        recognition_mode="high_accuracy",
        vision_pages_total=2,
        vision_pages_processed=2,
        vision_duration_ms=90,
    )
    assert detail.vision_pages_processed == 2


def test_workbench_sends_high_accuracy_form_field():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app/static/index.html").read_text(encoding="utf-8")
    javascript = (root / "app/static/js/app.js").read_text(encoding="utf-8")

    assert 'id="wb-high-accuracy"' in html
    assert "formData.append('recognition_mode'" in javascript
    assert "'high_accuracy'" in javascript
