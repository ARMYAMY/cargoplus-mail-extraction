from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import xlrd

from app.api.v1.extract import ALLOWED_UPLOAD_EXTENSIONS
from app.core.parser import parse_single_file
from app.core.parser.eml_parser import parse_eml
from app.core.parser.xls_parser import XlsParseError, parse_xls


class FakeSheet:
    def __init__(self, name, rows):
        self.name = name
        self._rows = rows
        self.nrows = len(rows)
        self.ncols = max((len(row) for row in rows), default=0)

    def cell(self, row_index, column_index):
        if column_index >= len(self._rows[row_index]):
            return SimpleNamespace(ctype=xlrd.XL_CELL_EMPTY, value="")
        return self._rows[row_index][column_index]


class FakeWorkbook:
    datemode = 0

    def __init__(self, sheets):
        self._sheets = sheets
        self.nsheets = len(sheets)
        self.released = False

    def sheets(self):
        return self._sheets

    def release_resources(self):
        self.released = True


def cell(ctype, value):
    return SimpleNamespace(ctype=ctype, value=value)


def test_parse_xls_formats_cells_and_multiple_sheets(tmp_path):
    file_path = tmp_path / "cargo.xls"
    file_path.write_bytes(b"legacy excel placeholder")
    workbook = FakeWorkbook([
        FakeSheet("货物", [
            [cell(xlrd.XL_CELL_TEXT, "件数"), cell(xlrd.XL_CELL_TEXT, "日期"), cell(xlrd.XL_CELL_TEXT, "确认")],
            [cell(xlrd.XL_CELL_NUMBER, 5.0), cell(xlrd.XL_CELL_DATE, 46000.0), cell(xlrd.XL_CELL_BOOLEAN, 1)],
            [cell(xlrd.XL_CELL_NUMBER, 44.44), cell(xlrd.XL_CELL_EMPTY, ""), cell(xlrd.XL_CELL_BOOLEAN, 0)],
        ]),
        FakeSheet("箱信息", [
            [cell(xlrd.XL_CELL_TEXT, "ContainerNo")],
            [cell(xlrd.XL_CELL_TEXT, "MSCU1234567")],
        ]),
    ])

    with patch("app.core.parser.xls_parser.xlrd.open_workbook", return_value=workbook):
        text, tables, ocr_text = parse_xls(file_path)

    expected_date = xlrd.xldate_as_datetime(46000.0, 0).date().isoformat()
    assert "### Sheet: 货物" in text
    assert "### Sheet: 箱信息" in text
    assert "| 5 | " in text
    assert "44.44" in text
    assert expected_date in text
    assert "TRUE" in text and "FALSE" in text
    assert tables[0]["rows"][1][0] == "5"
    assert tables[1]["rows"][1] == ["MSCU1234567"]
    assert ocr_text == ""
    assert workbook.released is True


def test_parse_xls_limits_rows_columns_cells_and_sheets(tmp_path):
    file_path = tmp_path / "large.xls"
    file_path.write_bytes(b"legacy excel placeholder")
    long_value = "A" * 2_100
    rows = [[cell(xlrd.XL_CELL_TEXT, long_value) for _ in range(101)] for _ in range(101)]
    workbook = FakeWorkbook([FakeSheet(f"S{i}", rows) for i in range(21)])

    with patch("app.core.parser.xls_parser.xlrd.open_workbook", return_value=workbook):
        _, tables, _ = parse_xls(file_path)

    assert len(tables) == 20
    assert len(tables[0]["rows"]) == 100
    assert len(tables[0]["rows"][0]) == 100
    assert len(tables[0]["rows"][0][0]) == 2_000


def test_parse_xls_keeps_merged_cell_anchor_and_empty_following_cells(tmp_path):
    file_path = tmp_path / "merged.xls"
    file_path.write_bytes(b"legacy excel placeholder")
    workbook = FakeWorkbook([
        FakeSheet("Merged", [
            [cell(xlrd.XL_CELL_TEXT, "SHIPPER"), cell(xlrd.XL_CELL_EMPTY, "")],
            [cell(xlrd.XL_CELL_TEXT, "ACME LOGISTICS"), cell(xlrd.XL_CELL_EMPTY, "")],
        ])
    ])

    with patch("app.core.parser.xls_parser.xlrd.open_workbook", return_value=workbook):
        _, tables, _ = parse_xls(file_path)

    assert tables[0]["rows"] == [["SHIPPER", ""], ["ACME LOGISTICS", ""]]


@pytest.mark.parametrize("payload", [b"", b"this is not an Excel workbook"])
def test_parse_xls_rejects_empty_or_invalid_files(tmp_path, payload):
    file_path = tmp_path / "invalid.xls"
    file_path.write_bytes(payload)

    with pytest.raises(XlsParseError, match="Excel解析失败"):
        parse_xls(file_path)


def test_parse_xls_reports_password_protection(tmp_path):
    file_path = tmp_path / "protected.xls"
    file_path.write_bytes(b"legacy excel placeholder")

    with patch("app.core.parser.xls_parser.xlrd.open_workbook", side_effect=xlrd.XLRDError("Workbook is encrypted")):
        with pytest.raises(XlsParseError, match="密码保护"):
            parse_xls(file_path)


def test_parse_single_file_dispatches_xls_and_propagates_failure(tmp_path):
    file_path = tmp_path / "cargo.xls"
    file_path.write_bytes(b"legacy excel placeholder")

    with patch("app.core.parser.parse_xls", return_value=("XLS TEXT", [{"sheet": "S", "rows": [["A"]]}], "")):
        attachment = parse_single_file(file_path)
    assert attachment.content_type == "application/vnd.ms-excel"
    assert attachment.text == "XLS TEXT"

    with patch("app.core.parser.parse_xls", side_effect=XlsParseError("Excel解析失败：文件损坏")):
        with pytest.raises(XlsParseError):
            parse_single_file(file_path)


def test_xls_is_allowed_for_direct_upload_and_eml_attachment(tmp_path):
    assert ".xls" in ALLOWED_UPLOAD_EXTENSIONS

    message = MIMEMultipart()
    message["Subject"] = "Legacy Excel"
    message.attach(MIMEText("Please process the attachment", "plain", "utf-8"))
    attachment = MIMEApplication(b"legacy excel bytes", Name="packing_list.xls")
    attachment["Content-Disposition"] = 'attachment; filename="packing_list.xls"'
    message.attach(attachment)
    eml_path = tmp_path / "mail.eml"
    eml_path.write_bytes(message.as_bytes())

    _, _, attachment_paths = parse_eml(eml_path, tmp_path / "attachments")
    assert len(attachment_paths) == 1
    assert attachment_paths[0].suffix == ".xls"


def test_xls_date_time_format_is_explicit(tmp_path):
    file_path = tmp_path / "time.xls"
    file_path.write_bytes(b"legacy excel placeholder")
    workbook = FakeWorkbook([
        FakeSheet("Times", [[cell(xlrd.XL_CELL_TEXT, "Time"), cell(xlrd.XL_CELL_DATE, 0.5)]])
    ])

    with patch("app.core.parser.xls_parser.xlrd.open_workbook", return_value=workbook):
        text, _, _ = parse_xls(file_path)

    assert "12:00:00" in text
