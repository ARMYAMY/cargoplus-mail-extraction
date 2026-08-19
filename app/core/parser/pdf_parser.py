import logging
from pathlib import Path
from typing import Any, List, Tuple
from pypdf import PdfReader
from app.core.parser.ocr_engine import extract_ocr_from_bytes

logger = logging.getLogger(__name__)


def parse_pdf(file_path: Path) -> Tuple[str, List[Any], str]:
    """
    Parses a PDF file.
    Returns: (text, tables, ocr_text)
    """
    text_parts = []
    tables = []
    ocr_parts = []

    try:
        reader = PdfReader(str(file_path))
        MAX_PAGES = 20
        pages_to_process = reader.pages[:MAX_PAGES]
        for page_idx, page in enumerate(pages_to_process):
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text.strip())
            else:
                # If page text is empty (scanned PDF), try extracting images and OCR (up to 3 images per page)
                for img_idx, img_file in enumerate(page.images[:3]):
                    try:
                        img_ocr = extract_ocr_from_bytes(img_file.data)
                        if img_ocr.strip():
                            ocr_parts.append(f"[Page {page_idx+1} Image {img_idx+1} OCR]:\n{img_ocr.strip()}")
                    except Exception as img_err:
                        logger.warning(f"Error OCR-ing image in PDF {file_path}: {img_err}")


    except Exception as e:
        logger.error(f"Failed to parse PDF {file_path}: {e}")
        return "", [], f"Error parsing PDF: {str(e)}"

    full_text = "\n\n".join(text_parts)
    full_ocr = "\n\n".join(ocr_parts)

    return full_text, tables, full_ocr
