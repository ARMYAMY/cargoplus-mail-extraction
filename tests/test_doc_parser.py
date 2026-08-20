import io
from pathlib import Path
from decimal import Decimal
import docx
import olefile
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import init_db
from app.core.parser.doc_parser import parse_doc
from app.core.parser import parse_single_file
from app.core.parser.eml_parser import parse_eml
from app.models.tenant import Tenant, ApiKey
from app.services.auth_service import generate_api_key_and_secret, hash_password


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


def test_parse_doc_empty_and_corrupt(tmp_path):
    empty_file = tmp_path / "empty.doc"
    empty_file.write_bytes(b"")
    text, tables, ocr = parse_doc(empty_file)
    assert text == ""
    assert tables == []

    corrupt_file = tmp_path / "corrupt.doc"
    corrupt_file.write_bytes(b"RANDOM_GARBAGE_BYTES_1234567890")
    text, tables, ocr = parse_doc(corrupt_file)
    assert "RANDOM_GARBAGE" in text


def test_parse_doc_misnamed_docx(tmp_path):
    # Create valid DOCX file saved with .doc extension
    doc = docx.Document()
    doc.add_heading("BOOKING CONFIRMATION", level=1)
    doc.add_paragraph("Carrier: COSCO SHIPPING")
    doc.add_paragraph("Booking No: COSU12345678")
    
    doc_path = tmp_path / "misnamed.doc"
    doc.save(str(doc_path))

    text, tables, ocr = parse_doc(doc_path)
    assert "BOOKING CONFIRMATION" in text
    assert "COSU12345678" in text
    assert "COSCO SHIPPING" in text


def test_parse_doc_rtf(tmp_path):
    rtf_content = rb"{\rtf1\ansi\deff0 {\fonttbl {\f0 Courier;}}\f0\fs24 SHIPPER: ABC LOGISTICS\par POD: ROTTERDAM\par}"
    rtf_path = tmp_path / "rtf_booking.doc"
    rtf_path.write_bytes(rtf_content)

    text, tables, ocr = parse_doc(rtf_path)
    assert "ABC LOGISTICS" in text
    assert "ROTTERDAM" in text


def test_parse_doc_html_xml(tmp_path):
    xml_content = "<html><body><h1>BOOKING ADVICE</h1><p>Vessel: EVER GIVEN</p><p>POL: SHANGHAI</p></body></html>"
    xml_path = tmp_path / "advice.doc"
    xml_path.write_text(xml_content, encoding="utf-8")

    text, tables, ocr = parse_doc(xml_path)
    assert "BOOKING ADVICE" in text
    assert "EVER GIVEN" in text
    assert "SHANGHAI" in text


def test_parse_doc_binary_ole(tmp_path):
    # Build an OLE2 structured file in memory with WordDocument stream
    bio = io.BytesIO()
    # Construct synthetic WordDocument stream with UTF-16LE text and ASCII runs
    sample_text = "BOOKING MSC / MSCU9876543 / 2x40HQ / SHANGHAI TO HAMBURG"
    encoded_utf16 = sample_text.encode("utf-16le")
    # Add some padding bytes simulating OLE Word header
    word_stream_data = b"\xec\xa5\x00\x00" + b"\x00" * 512 + encoded_utf16 + b"\x00" * 128
    
    # We can write an OLE2 file using olefile or simulate text extraction
    ole_path = tmp_path / "legacy_booking.doc"
    # Write synthetic binary with OLE magic header
    ole_path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504 + encoded_utf16)

    text, tables, ocr = parse_doc(ole_path)
    assert "MSCU9876543" in text or "BOOKING MSC" in text


def test_parse_single_file_doc_dispatch(tmp_path):
    doc = docx.Document()
    doc.add_paragraph("SHIPPER: GLOBAL FREIGHT LTD")
    doc.add_paragraph("CONTAINER: TGHU1234567 40HQ")
    doc_path = tmp_path / "test_booking.doc"
    doc.save(str(doc_path))

    att = parse_single_file(doc_path)
    assert att.filename == "test_booking.doc"
    assert att.content_type == "application/msword"
    assert "GLOBAL FREIGHT LTD" in att.text
    assert "TGHU1234567" in att.text


def test_eml_parser_with_doc_attachment(tmp_path):
    # Construct an EML email containing a .doc attachment
    eml_file = tmp_path / "booking_with_doc.eml"
    doc_file = tmp_path / "attachment.doc"
    doc = docx.Document()
    doc.add_paragraph("Booking Ref: MSK889900")
    doc.save(str(doc_file))

    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "Fwd: Booking Advice"
    msg["From"] = "carrier@shipping.com"
    msg["To"] = "agent@cargoplus.com"
    msg.set_content("Please find attached booking document in doc format.")
    
    doc_data = doc_file.read_bytes()
    msg.add_attachment(doc_data, maintype="application", subtype="msword", filename="booking_advice.doc")
    
    with open(eml_file, "wb") as f:
        f.write(msg.as_bytes())

    output_dir = tmp_path / "extracted_att"
    output_dir.mkdir()
    
    subject, body, extracted_files = parse_eml(eml_file, output_dir)
    assert "Booking Advice" in subject
    assert len(extracted_files) == 1
    assert extracted_files[0].name.endswith("booking_advice.doc")
    
    # Verify parsed attachment
    att = parse_single_file(extracted_files[0])
    assert "MSK889900" in att.text


@pytest.mark.asyncio
async def test_extract_async_upload_with_doc(tmp_path):
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register and get token
        reg_res = await client.post(
            "/api/v1/auth/register",
            json={
                "company_name": f"DocTester_{Path(tmp_path).name[:8]}",
                "contact_email": f"doc_{Path(tmp_path).name[:8]}@example.com",
                "password": "Password123!",
            },
        )
        assert reg_res.status_code == 200
        tenant_id = reg_res.json()["data"]["tenant_id"]
        api_key = reg_res.json()["data"]["api_key"]

        # Audit activate
        from app.config import settings
        await client.put(
            f"/admin/tenants/{tenant_id}/status?is_active=true",
            headers={"X-Admin-Secret": settings.ADMIN_SECRET_KEY},
        )

        # Create a sample .doc file
        doc = docx.Document()
        doc.add_paragraph("BOOKING NO: ONEY1234567")
        doc.add_paragraph("VESSEL: ONE APUS / 012E")
        doc.add_paragraph("POL: NINGBO")
        doc.add_paragraph("POD: LONG BEACH")
        sample_doc = tmp_path / "booking_note.doc"
        doc.save(str(sample_doc))

        with open(sample_doc, "rb") as f:
            upload_res = await client.post(
                "/api/v1/extract/async/upload",
                headers={"Authorization": f"Bearer {api_key}"},
                files=[("files", ("booking_note.doc", f, "application/msword"))],
                data={"mail_subject": "Booking advice .doc upload"},
            )
        assert upload_res.status_code == 200
        res_json = upload_res.json()
        assert "task_id" in res_json
        assert res_json["status"] in {"PENDING", "PROCESSING", "SUCCESS"}
