import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.core.parser.ocr_engine import get_ocr_engine, extract_ocr_from_image, extract_ocr_from_bytes
import app.core.parser.ocr_engine as ocr_module
from app.core.parser import parse_single_file, process_uploaded_files
from app.services.storage_service import StorageService


def test_ocr_engine_all_paths(tmp_path):
    # 1. get_ocr_engine failure fallback
    with patch("builtins.__import__", side_effect=ImportError("No RapidOCR")):
        ocr_module._ocr_instance = None
        eng_none = get_ocr_engine()
        assert eng_none is None

    # 2. extract_ocr_from_image when engine is None
    dummy_img = tmp_path / "test.png"
    dummy_img.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert extract_ocr_from_image(dummy_img) == ""
    assert extract_ocr_from_bytes(b"\x89PNG") == ""

    # 3. extract_ocr_from_image success with mock engine
    mock_ocr_engine = MagicMock()
    mock_ocr_engine.return_value = (
        [[[0, 0], "CONTAINER NO: MSCU1234567", 0.95], [[0, 1], "PORT: SHANGHAI", 0.99]],
        [0.05],
    )
    ocr_module._ocr_instance = mock_ocr_engine

    txt_img = extract_ocr_from_image(dummy_img)
    assert "CONTAINER NO: MSCU1234567" in txt_img
    assert "PORT: SHANGHAI" in txt_img

    txt_bytes = extract_ocr_from_bytes(b"fake_bytes")
    assert "CONTAINER NO: MSCU1234567" in txt_bytes

    # 4. extract_ocr_from_image empty result
    mock_ocr_engine.return_value = (None, [0.01])
    assert extract_ocr_from_image(dummy_img) == ""
    assert extract_ocr_from_bytes(b"fake_bytes") == ""

    # 5. extract_ocr_from_image exception
    mock_ocr_engine.side_effect = RuntimeError("ONNX Runtime internal crash")
    assert extract_ocr_from_image(dummy_img) == ""
    assert extract_ocr_from_bytes(b"fake_bytes") == ""

    ocr_module._ocr_instance = None


def test_parser_dispatcher_image_and_docs(tmp_path):
    # 1. Image parsing dispatcher
    img_file = tmp_path / "sample.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0")

    with patch("app.core.parser.extract_ocr_from_image", return_value="SHANGHAI 20GP"):
        att_img = parse_single_file(img_file)
        assert att_img.content_type == "image/jpg"
        assert att_img.ocr_text == "SHANGHAI 20GP"

    # 2. Docx and xlsx dispatchers
    docx_file = tmp_path / "test.docx"
    docx_file.write_bytes(b"PK\x03\x04")
    with patch("app.core.parser.parse_word", return_value=("Doc text", [], "Doc OCR")):
        att_docx = parse_single_file(docx_file)
        assert att_docx.text == "Doc text"

    xlsx_file = tmp_path / "test.xlsx"
    xlsx_file.write_bytes(b"PK\x03\x04")
    with patch("app.core.parser.parse_excel", return_value=("Excel text", [], "Excel OCR")):
        att_xlsx = parse_single_file(xlsx_file)
        assert att_xlsx.text == "Excel text"


@pytest.mark.asyncio
async def test_storage_service_worker_loop():
    # Test background retention worker exception handling and single iteration
    call_count = 0

    def mock_prune(days):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Pruning simulation error")
        return 0

    with patch.object(StorageService, "prune_expired_uploads", side_effect=mock_prune), \
         patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        with pytest.raises(asyncio.CancelledError):
            await StorageService.start_retention_pruning_worker()
        assert call_count >= 1
