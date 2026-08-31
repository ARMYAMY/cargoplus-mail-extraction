import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List, Tuple

import xlrd
from xlrd.biffh import XLRDError

logger = logging.getLogger(__name__)

MAX_SHEETS = 20
MAX_SHEET_ROWS = 100
MAX_SHEET_COLUMNS = 100
MAX_CELL_CHARS = 2_000


class XlsParseError(ValueError):
    """Raised when a legacy Excel workbook cannot be safely parsed."""


def _format_datetime(value: float, datemode: int) -> str:
    parsed = xlrd.xldate_as_datetime(value, datemode)
    if parsed.date() in {datetime(1899, 12, 31).date(), datetime(1904, 1, 1).date()}:
        if parsed.microsecond:
            return parsed.time().isoformat(timespec="microseconds").rstrip("0").rstrip(".")
        return parsed.time().isoformat(timespec="seconds")
    if parsed.time() == datetime.min.time():
        return parsed.date().isoformat()
    if parsed.microsecond:
        return parsed.isoformat(sep=" ", timespec="microseconds").rstrip("0").rstrip(".")
    return parsed.isoformat(sep=" ", timespec="seconds")


def _format_cell(cell: Any, datemode: int) -> str:
    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return ""
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if bool(cell.value) else "FALSE"
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return _format_datetime(cell.value, datemode)
        except (TypeError, ValueError, XLRDError):
            return str(cell.value)[:MAX_CELL_CHARS]
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        number = float(cell.value)
        return str(int(number)) if number.is_integer() else str(number)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, f"#ERROR:{cell.value}")
    return str(cell.value).strip()[:MAX_CELL_CHARS]


def _build_markdown(sheetname: str, rows: List[List[str]]) -> str:
    headers = rows[0]
    header_line = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"
    data_lines = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        data_lines.append("| " + " | ".join(padded[:len(headers)]) + " |")
    return f"### Sheet: {sheetname}\n{header_line}\n{separator}\n" + "\n".join(data_lines)


def _friendly_error(exc: Exception) -> XlsParseError:
    detail = str(exc).strip()
    lowered = detail.casefold()
    if "password" in lowered or "encrypted" in lowered:
        message = "Excel解析失败：暂不支持密码保护的 .xls 文件"
    else:
        message = "Excel解析失败：文件损坏、格式不正确或并非有效的 .xls 文件"
    logger.error("Failed to parse legacy Excel workbook: %s", detail or exc.__class__.__name__)
    return XlsParseError(message)


def parse_xls(file_path: Path) -> Tuple[str, List[Any], str]:
    """Parse a legacy Excel 97-2003 workbook into the standard attachment shape."""
    if not file_path.is_file() or file_path.stat().st_size == 0:
        logger.error("Failed to parse legacy Excel %s: empty or missing file", file_path)
        raise XlsParseError("Excel解析失败：文件为空或不存在")

    try:
        workbook = xlrd.open_workbook(str(file_path), on_demand=True)
    except Exception as exc:
        raise _friendly_error(exc) from exc

    text_blocks: List[str] = []
    tables: List[Any] = []
    try:
        if workbook.nsheets == 0:
            logger.error("Failed to parse legacy Excel %s: workbook has no worksheets", file_path)
            raise XlsParseError("Excel解析失败：工作簿中没有工作表")

        for sheet in workbook.sheets():
            rows: List[List[str]] = []
            row_limit = min(sheet.nrows, MAX_SHEET_ROWS)
            column_limit = min(sheet.ncols, MAX_SHEET_COLUMNS)
            for row_index in range(row_limit):
                row = [
                    _format_cell(sheet.cell(row_index, column_index), workbook.datemode)[:MAX_CELL_CHARS]
                    for column_index in range(column_limit)
                ]
                if any(row):
                    rows.append(row)

            if not rows:
                continue

            text_blocks.append(_build_markdown(sheet.name, rows))
            tables.append({"sheet": sheet.name, "rows": rows})
            if len(tables) >= MAX_SHEETS:
                break
    except XlsParseError:
        raise
    except Exception as exc:
        raise _friendly_error(exc) from exc
    finally:
        workbook.release_resources()

    if not tables:
        logger.error("Failed to parse legacy Excel %s: workbook contains no readable cells", file_path)
        raise XlsParseError("Excel解析失败：工作簿中没有可读取的单元格内容")

    return "\n\n".join(text_blocks), tables, ""
