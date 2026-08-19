from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from app.core.parser import parse_single_file, process_uploaded_files, compress_text_content
from app.core.parser.eml_parser import parse_eml, html_to_plain_text
from app.core.parser.excel_parser import parse_excel
from app.core.parser.word_parser import parse_word
from app.core.parser.pdf_parser import parse_pdf
from app.core.parser.ocr_engine import extract_ocr_from_image, extract_ocr_from_bytes, get_ocr_engine


def test_compress_text_content():
    # Short text
    assert compress_text_content("Short cargo text", 100) == "Short cargo text"

    # Cargo keyword prioritization
    text = "\n".join([
        "Irrelevant chatter line 1",
        "BOOKING NO: MSC123456",
        "Irrelevant chatter line 2",
        "POL: YANTIAN",
        "POD: ROTTERDAM",
        "CONTAINER: MSCU9988776",
        "Irrelevant chatter line 3",
    ])
    compressed = compress_text_content(text, max_chars=80)
    assert "BOOKING NO" in compressed or "POL" in compressed


def test_html_to_plain_text():
    html_sample = "<p>Hello <b>World</b><br>Booking Ref: 12345</p>"
    plain = html_to_plain_text(html_sample)
    assert "Hello World" in plain
    assert "Booking Ref: 12345" in plain
    assert "<" not in plain


def test_eml_parser_multipart_and_attachments(tmp_path):
    # Construct a real RFC 822 EML message with body and attachment
    msg = MIMEMultipart()
    msg["Subject"] = "Booking Confirmation COSCO"
    msg["From"] = "carrier@cosco.com"
    msg["To"] = "ops@freight.com"

    text_part = MIMEText("Please confirm booking for container CCLU9988776.", "plain", "utf-8")
    html_part = MIMEText("<p>HTML version of the booking</p>", "html", "utf-8")
    msg.attach(text_part)
    msg.attach(html_part)

    # Attach a text file
    att = MIMEApplication(b"PACKING LIST CONTENT: 500 CARTONS", Name="packing_list.txt")
    att["Content-Disposition"] = 'attachment; filename="packing_list.txt"'
    msg.attach(att)

    eml_file = tmp_path / "test_email.eml"
    eml_file.write_bytes(msg.as_bytes())

    subj, body, extracted_att_paths = parse_eml(eml_file, tmp_path)
    assert "COSCO" in subj
    assert "CCLU9988776" in body
    assert len(extracted_att_paths) >= 1


def test_process_uploaded_files_and_parse_single_file(tmp_path):
    # 1. Text file
    txt_file = tmp_path / "booking_memo.txt"
    txt_file.write_text("POL: SHANGHAI\nPOD: HAMBURG\nCONTAINER: CCLU1234567", encoding="utf-8")

    att = parse_single_file(txt_file)
    assert att.filename == "booking_memo.txt"
    assert "SHANGHAI" in att.text

    # 2. Process uploaded files
    payload = process_uploaded_files(
        file_paths=[txt_file],
        subject="Email Subject",
        body="Email Body",
        temp_dir=tmp_path,
    )
    assert payload.mail_subject == "Email Subject"
    assert "Email Body" in payload.mail_body
    assert len(payload.attachments) == 1


def test_pdf_parser_mocked(tmp_path):
    dummy_pdf = tmp_path / "dummy.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy")

    with patch("app.core.parser.pdf_parser.PdfReader") as mock_reader:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "BILL OF LADING\nVESSEL: EVER GIVEN\nPOL: SHANGHAI"
        mock_page.images = []
        mock_reader.return_value.pages = [mock_page]

        text, tables, ocr_text = parse_pdf(dummy_pdf)
        assert "EVER GIVEN" in text


def test_word_parser_mocked(tmp_path):
    dummy_docx = tmp_path / "dummy.docx"
    dummy_docx.write_bytes(b"PK\x03\x04 dummy docx")

    with patch("app.core.parser.word_parser.Document") as mock_docx:
        p1 = MagicMock(text="CARGO SHIPPING ORDER")
        table = MagicMock()
        row = MagicMock()
        cell1 = MagicMock(text="Container No")
        cell2 = MagicMock(text="MSCU1234567")
        row.cells = [cell1, cell2]
        table.rows = [row]

        mock_instance = MagicMock()
        mock_instance.paragraphs = [p1]
        mock_instance.tables = [table]
        mock_docx.return_value = mock_instance

        text, tables, ocr_text = parse_word(dummy_docx)
        assert "CARGO SHIPPING ORDER" in text
        assert len(tables) >= 1


def test_ocr_engine_fallback(tmp_path):
    # When RapidOCR is not loaded or mocked
    with patch("app.core.parser.ocr_engine.get_ocr_engine", return_value=None):
        res_bytes = extract_ocr_from_bytes(b"dummy image bytes")
        assert res_bytes == ""

        dummy_img = tmp_path / "sample.png"
        dummy_img.write_bytes(b"\x89PNG dummy")
        res_img = extract_ocr_from_image(dummy_img)
        assert res_img == ""
