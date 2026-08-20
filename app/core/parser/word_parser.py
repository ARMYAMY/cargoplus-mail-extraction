import logging
from pathlib import Path
from typing import Any, List, Tuple
from docx import Document
from app.core.parser.ocr_engine import extract_ocr_from_bytes
from app.services.vision_service import VisionBudget, VisionService

logger = logging.getLogger(__name__)


def parse_word(
    file_path: Path,
    vision_budget: VisionBudget | None = None,
) -> Tuple[str, List[Any], str]:
    """
    Parses a Word (.docx) document.
    Extracts text, structured tables, and transcribes embedded document screenshots/images via Vision LLM.
    Returns: (text, tables, ocr_text)
    """
    paragraphs = []
    tables = []
    ocr_parts = []

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

        # Extract embedded images / screenshots from Word package
        if vision_budget is None:
            from app.config import settings

            vision_budget = VisionBudget(settings.VISION_MAX_IMAGES_PER_TASK)
        if hasattr(doc, "part") and hasattr(doc.part, "related_parts"):
            for rel_id, rel_part in doc.part.related_parts.items():
                if vision_budget.exhausted:
                    logger.info("Reached task-wide image transcription limit in Word %s", file_path.name)
                    break

                content_type = getattr(rel_part, "content_type", "").lower()
                part_name = str(getattr(rel_part, "partname", "")).lower()
                if "image" in content_type or "image" in part_name:
                    img_bytes = getattr(rel_part, "blob", None)
                    if img_bytes and VisionService.is_valid_document_image(img_bytes):
                        if not vision_budget.try_acquire():
                            break
                        try:
                            img_text = VisionService.transcribe_image_sync(
                                img_bytes,
                                filename_hint=f"{file_path.name}_embedded_{rel_id}",
                                custom_timeout=vision_budget.request_timeout(),
                            )
                            if img_text.strip():
                                ocr_parts.append(
                                    f"[Word文档内嵌单证截图/图片识别内容]:\n{img_text.strip()}"
                                )
                        except Exception as img_err:
                            logger.warning(f"Error transcribing Word embedded image in {file_path.name}: {img_err}")

    except Exception as e:
        logger.error(f"Failed to parse Word document {file_path}: {e}")
        return "", [], f"Error parsing Word: {str(e)}"

    full_text = "\n\n".join(paragraphs)
    full_ocr = "\n\n".join(ocr_parts)

    return full_text, tables, full_ocr
