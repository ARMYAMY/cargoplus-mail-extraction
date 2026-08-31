import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from pypdf import PdfReader
from app.services.vision_service import VisionBudget, VisionService

logger = logging.getLogger(__name__)


def parse_pdf(
    file_path: Path,
    vision_budget: VisionBudget | None = None,
    stage_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[str, List[Any], str]:
    """
    Parses a PDF file.
    Extracts digital text and transcribes embedded images/scans via Vision LLM (with RapidOCR fallback).
    Returns: (text, tables, ocr_text)
    """
    text_parts = []
    tables = []
    ocr_parts = []

    try:
        reader = PdfReader(str(file_path))
        MAX_PAGES = 20
        if vision_budget is None:
            from app.config import settings

            vision_budget = VisionBudget(settings.VISION_MAX_IMAGES_PER_TASK)

        pages_to_process = reader.pages[:MAX_PAGES]
        for page_idx, page in enumerate(pages_to_process):
            # Layout mode preserves horizontal spacing and empty columns in
            # form-style shipping documents.  Plain extraction can collapse a
            # row such as ``POR | POL | POD | FPOD`` and shift all non-empty
            # values to the left, changing their business meaning.
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except (TypeError, ValueError, NotImplementedError):
                # Keep compatibility with older pypdf releases and unusual
                # pages that cannot be processed by the layout extractor.
                page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text.strip())

            # Check for embedded images (both on scanned pages and hybrid pages)
            if hasattr(page, "images") and page.images and not vision_budget.exhausted:
                for img_idx, img_file in enumerate(page.images):
                    if vision_budget.exhausted:
                        logger.info(
                            "Reached task-wide image transcription limit in PDF %s",
                            file_path.name,
                        )
                        break

                    if stage_callback:
                        stage_callback("PDF_TO_IMAGE", {
                            "filename": file_path.name,
                            "page": page_idx + 1,
                            "image": img_idx + 1,
                        })
                    img_bytes = img_file.data
                    if VisionService.is_valid_document_image(img_bytes):
                        if not vision_budget.try_acquire():
                            break
                        try:
                            if stage_callback:
                                stage_callback("VISION_OCR", {
                                    "filename": file_path.name,
                                    "page": page_idx + 1,
                                    "image": img_idx + 1,
                                })
                            img_transcription = VisionService.transcribe_image_sync(
                                img_bytes,
                                filename_hint=f"{file_path.name}_p{page_idx+1}_img{img_idx+1}",
                                custom_timeout=vision_budget.request_timeout(),
                            )
                            if img_transcription.strip():
                                ocr_parts.append(
                                    f"[PDF第{page_idx+1}页 单证扫描/内嵌图识别内容]:\n{img_transcription.strip()}"
                                )
                        except Exception as img_err:
                            logger.warning(
                                f"Error transcribing embedded image in PDF {file_path.name}: {img_err}"
                            )

    except Exception as e:
        logger.error(f"Failed to parse PDF {file_path}: {e}")
        return "", [], f"Error parsing PDF: {str(e)}"

    full_text = "\n\n".join(text_parts)
    full_ocr = "\n\n".join(ocr_parts)

    return full_text, tables, full_ocr
