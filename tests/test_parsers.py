import openpyxl
from app.core.parser.excel_parser import parse_excel
from app.core.parser.eml_parser import html_to_plain_text
from app.core.parser import parse_single_file


def test_html_to_plain_text():
    html_content = "<p>Dear Team,</p><p>Please arrange booking for <b>500 cartons</b>.<br>Port: Yantian</p>"
    text = html_to_plain_text(html_content)
    assert "Dear Team," in text
    assert "500 cartons" in text
    assert "Port: Yantian" in text
    assert "<p>" not in text


def test_excel_parser(tmp_path):
    # Create sample excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CargoList"
    ws.append(["ContainerNo", "SealNo", "Package", "Weight", "Volume"])
    ws.append(["MSCU1234567", "SEAL999", "100 CTNS", "2500 KGS", "15 CBM"])
    excel_path = tmp_path / "packing_list.xlsx"
    wb.save(excel_path)

    text, tables, ocr_text = parse_excel(excel_path)
    assert "MSCU1234567" in text
    assert len(tables) == 1
    assert tables[0]["rows"][0] == ["ContainerNo", "SealNo", "Package", "Weight", "Volume"]


def test_parse_single_file_txt(tmp_path):
    sample_txt = tmp_path / "booking_note.txt"
    sample_txt.write_text("SHIPPER: TEST FORWARDER", encoding="utf-8")
    att = parse_single_file(sample_txt)
    assert att.filename == "booking_note.txt"
    assert "SHIPPER: TEST FORWARDER" in att.text
