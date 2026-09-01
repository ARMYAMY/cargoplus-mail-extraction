import logging
import io
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import pypdfium2 as pdfium
from pypdf import PdfReader
from app.config import settings
from app.services.vision_service import (
    HighAccuracyVisionError,
    VisionBudget,
    VisionService,
)

logger = logging.getLogger(__name__)


def parse_pdf(
    file_path: Path,
    vision_budget: VisionBudget | None = None,
    stage_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    recognition_mode: str = "standard",
    vision_report: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Any], str]:
    """
    Parses a PDF file.
    Extracts digital text and transcribes embedded images/scans via Vision LLM (with RapidOCR fallback).
    Returns: (text, tables, ocr_text)
    """
    text_parts = []
    tables = []
    ocr_parts = []
    rendered_document = None

    try:
        reader = PdfReader(str(file_path))
        max_pages = settings.HIGH_ACCURACY_MAX_PAGES if recognition_mode == "high_accuracy" else 20
        if vision_budget is None:
            vision_budget = VisionBudget(settings.VISION_MAX_IMAGES_PER_TASK)

        pages_to_process = reader.pages[:max_pages]
        if recognition_mode == "high_accuracy":
            if vision_report is not None:
                vision_report["pages_total"] = vision_report.get("pages_total", 0) + len(pages_to_process)
            if len(reader.pages) > max_pages:
                raise HighAccuracyVisionError(f"高精度视觉识别最多处理 {max_pages} 页，请拆分文件后重试")

        rendered_document = pdfium.PdfDocument(str(file_path)) if recognition_mode == "high_accuracy" else None
        for page_idx, page in enumerate(pages_to_process):
            # Layout mode preserves horizontal spacing and empty columns in
            # form-style shipping documents.  Plain extraction can collapse a
            # row such as ``POR | POL | POD | FPOD`` and shift all non-empty
            # values to the left, changing their business meaning.
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except (TypeError, ValueError, NotImplementedError, KeyError):
                # Keep compatibility with older pypdf releases and unusual
                # pages that cannot be processed by the layout extractor.
                try:
                    page_text = page.extract_text() or ""
                except Exception as text_error:
                    logger.warning(
                        "Native text extraction failed for PDF %s page %s: %s",
                        file_path.name,
                        page_idx + 1,
                        text_error,
                    )
                    page_text = ""
            if page_text.strip():
                text_parts.append(page_text.strip())

            if recognition_mode == "high_accuracy":
                if not vision_budget.try_acquire():
                    raise HighAccuracyVisionError("高精度视觉识别页数或总耗时超过本次任务限制")
                if stage_callback:
                    stage_callback("PDF_TO_IMAGE", {"filename": file_path.name, "page": page_idx + 1})
                page_started = time.monotonic()
                pdfium_page = rendered_document[page_idx]
                bitmap = pdfium_page.render(scale=200 / 72)
                pil_image = bitmap.to_pil()
                image_buffer = io.BytesIO()
                pil_image.save(image_buffer, format="PNG")
                pil_image.close()
                bitmap.close()
                pdfium_page.close()
                if stage_callback:
                    stage_callback("VISION_OCR", {"filename": file_path.name, "page": page_idx + 1})
                img_transcription = VisionService.transcribe_image_sync(
                    image_buffer.getvalue(),
                    filename_hint=f"{file_path.name}_p{page_idx + 1}",
                    custom_timeout=vision_budget.request_timeout(),
                    enabled=True,
                    allow_local_fallback=False,
                )
                if not img_transcription.strip():
                    raise HighAccuracyVisionError(f"高精度视觉模型未返回第 {page_idx + 1} 页的识别文本")
                ocr_parts.append(
                    f"[PDF第{page_idx + 1}页 高精度视觉识别内容]:\n{img_transcription.strip()}"
                )
                if vision_report is not None:
                    vision_report["pages_processed"] = vision_report.get("pages_processed", 0) + 1
                    vision_report["duration_ms"] = vision_report.get("duration_ms", 0) + int(
                        (time.monotonic() - page_started) * 1000
                    )
                continue

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

    except HighAccuracyVisionError:
        raise
    except Exception as e:
        logger.error(f"Failed to parse PDF {file_path}: {e}")
        if recognition_mode == "high_accuracy":
            raise HighAccuracyVisionError(f"高精度 PDF 页面渲染失败: {e}") from e
        return "", [], f"Error parsing PDF: {str(e)}"
    finally:
        if rendered_document is not None:
            rendered_document.close()

    full_text = "\n\n".join(text_parts)
    full_ocr = "\n\n".join(ocr_parts)

    return full_text, tables, full_ocr
