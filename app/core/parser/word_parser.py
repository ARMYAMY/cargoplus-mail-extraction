import logging
from pathlib import Path
from typing import Any, List, Tuple
from docx import Document

logger = logging.getLogger(__name__)


def parse_word(file_path: Path) -> Tuple[str, List[Any], str]:
    """
    Parses a Word (.docx) document.
    Returns: (text, tables, ocr_text)
    """
    paragraphs = []
    tables = []

    try:
        doc = Document(str(file_path))
        for p in doc.paragraphs[:5_000]:
            if p.text.strip():
                paragraphs.append(p.text.strip()[:5_000])

        for tbl_idx, table in enumerate(doc.tables[:20]):
            table_rows = []
            for row in table.rows[:200]:
                row_data = [cell.text.strip()[:2_000] for cell in row.cells[:100]]
                if any(row_data):
                    table_rows.append(row_data)

            if table_rows:
                tables.append({"table_index": tbl_idx, "rows": table_rows})
                # Append table as text
                headers = table_rows[0]
                lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
                for r in table_rows[1:]:
                    padded = r + [""] * (len(headers) - len(r))
                    lines.append("| " + " | ".join(padded[:len(headers)]) + " |")
                paragraphs.append("\n".join(lines))

    except Exception as e:
        logger.error(f"Failed to parse Word document {file_path}: {e}")
        return "", [], f"Error parsing Word: {str(e)}"

    return "\n\n".join(paragraphs), tables, ""
