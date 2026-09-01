import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional
from app.schemas.task import AttachmentInput, SkillV3InputPayload
from app.core.parser.eml_parser import parse_eml
from app.core.parser.pdf_parser import parse_pdf
from app.core.parser.excel_parser import parse_excel
from app.core.parser.xls_parser import XlsParseError, parse_xls
from app.core.parser.word_parser import parse_word
from app.core.parser.doc_parser import DocParseError, parse_doc
from app.core.parser.ocr_engine import extract_ocr_from_image
from app.services.vision_service import HighAccuracyVisionError, VisionBudget, VisionService

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}

# Cargo keyword patterns for prioritizing high-value context
CARGO_KEYWORDS = re.compile(
    r"(booking|bkg|shipper|consignee|notify|pol|pod|port|vessel|voyage|container|cntr|seal|gross weight|measurement|cbm|kgs|pcs|cartons|packages|hs code|freight|commodity|goods)",
    re.IGNORECASE,
)


def compress_text_content(text: str, max_chars: int = 8000) -> str:
    """
    Intelligently compresses long document text while preserving high-value cargo details.
    """
    if not text or len(text) <= max_chars:
        return text

    # Select cargo-related lines first, but restore their original order before
    # returning the text.  Reordering all labels ahead of their values breaks
    # form semantics, and stripping leading whitespace destroys PDF columns.
    lines = []
    for index, raw_line in enumerate(text.splitlines()):
        preserved = raw_line.rstrip()
        probe = preserved.strip()
        if probe:
            lines.append((index, preserved, bool(CARGO_KEYWORDS.search(probe))))

    selected = set()
    used = 0
    for priority in (True, False):
        for index, line, is_priority in lines:
            if is_priority != priority:
                continue
            cost = len(line) + (1 if selected else 0)
            if used + cost > max_chars:
                continue
            selected.add(index)
            used += cost

    compressed = "\n".join(
        line for index, line, _ in lines if index in selected
    )
    if not compressed and lines:
        # A single very long line may not fit the line budget. Preserve its
        # beginning instead of returning an empty attachment.
        priority_line = next((line for _, line, flag in lines if flag), lines[0][1])
        return priority_line[:max_chars]
    return compressed[:max_chars]


def parse_single_file(
    file_path: Path,
    vision_budget: VisionBudget | None = None,
    stage_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    recognition_mode: str = "standard",
    vision_report: Optional[Dict[str, Any]] = None,
) -> AttachmentInput:
    """Parses an individual file and returns an AttachmentInput structure."""
    ext = file_path.suffix.lower()
    filename = file_path.name
    content_type = "application/octet-stream"
    text = ""
    tables = []
    ocr_text = ""

    try:
        if ext == ".pdf":
            content_type = "application/pdf"
            text, tables, ocr_text = parse_pdf(
                file_path, vision_budget, stage_callback, recognition_mode, vision_report
            )
        elif ext == ".xlsx":
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            text, tables, ocr_text = parse_excel(file_path)
        elif ext == ".xls":
            content_type = "application/vnd.ms-excel"
            text, tables, ocr_text = parse_xls(file_path)
        elif ext == ".docx":
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            text, tables, ocr_text = parse_word(
                file_path,
                vision_budget,
                recognition_mode,
                vision_report,
                stage_callback,
            )
        elif ext == ".doc":
            content_type = "application/msword"
            text, tables, ocr_text = parse_doc(file_path, vision_budget)
        elif ext in IMAGE_EXTENSIONS:
            content_type = f"image/{ext.lstrip('.')}"
            if stage_callback:
                stage_callback("VISION_OCR", {"filename": filename, "page": 1, "image": 1})
            if recognition_mode == "high_accuracy":
                if not vision_budget.try_acquire():
                    raise HighAccuracyVisionError("高精度视觉识别页数或总耗时超过本次任务限制")
                if vision_report is not None:
                    vision_report["pages_total"] = vision_report.get("pages_total", 0) + 1
                started = time.monotonic()
                ocr_text = VisionService.transcribe_image_sync(
                    file_path.read_bytes(),
                    filename_hint=filename,
                    custom_timeout=vision_budget.request_timeout(),
                    enabled=True,
                    allow_local_fallback=False,
                )
                if not ocr_text.strip():
                    raise HighAccuracyVisionError("高精度视觉模型未返回图片识别文本")
                if vision_report is not None:
                    vision_report["pages_processed"] = vision_report.get("pages_processed", 0) + 1
                    vision_report["duration_ms"] = vision_report.get("duration_ms", 0) + int(
                        (time.monotonic() - started) * 1000
                    )
            else:
                ocr_text = extract_ocr_from_image(file_path, vision_budget)
        elif ext in {".txt", ".csv", ".json", ".md"}:
            content_type = "text/plain"
            text = file_path.read_text(encoding="utf-8", errors="replace")
        else:
            text = f"[Binary or unsupported attachment format: {filename}]"
    except (DocParseError, XlsParseError, HighAccuracyVisionError):
        # Malformed legacy Office files must stop before invoking or billing the LLM.
        raise
    except Exception as e:
        logger.error(f"Error parsing file {file_path}: {e}")
        text = f"[Error parsing attachment {filename}: {str(e)}]"

    # Smart compression to prevent LLM context overflow and timeouts
    text = compress_text_content(text, max_chars=8000)
    ocr_text = compress_text_content(
        ocr_text,
        max_chars=18000 if recognition_mode == "high_accuracy" else 6000,
    )

    return AttachmentInput(
        filename=filename,
        content_type=content_type,
        text=text,
        tables=tables[:20],  # Keep up to top 20 relevant tables
        ocr_text=ocr_text,
    )


def process_uploaded_files(
    file_paths: List[Path],
    subject: str = "",
    body: str = "",
    temp_dir: Path = None,
    vision_budget: VisionBudget | None = None,
    stage_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    recognition_mode: str = "standard",
    vision_report: Optional[Dict[str, Any]] = None,
) -> SkillV3InputPayload:
    """
    Processes uploaded files (which might include an .eml email file and/or various attachments),
    and converts them into a consolidated SkillV3InputPayload.
    """
    final_subject = subject
    final_body = body
    attachments: List[AttachmentInput] = []
    if vision_budget is None:
        from app.config import settings

        vision_budget = VisionBudget(settings.VISION_MAX_IMAGES_PER_TASK)

    for file_path in file_paths:
        ext = file_path.suffix.lower()
        if ext == ".eml":
            # Parse email file
            out_dir = temp_dir if temp_dir else file_path.parent
            eml_subj, eml_body, extracted_att_paths = parse_eml(file_path, out_dir)
            if not final_subject:
                final_subject = eml_subj
            if eml_body:
                final_body = (final_body + "\n\n" + eml_body).strip() if final_body else eml_body

            # Parse each extracted attachment
            for att_p in extracted_att_paths:
                attachments.append(
                    parse_single_file(
                        att_p, vision_budget, stage_callback, recognition_mode, vision_report
                    )
                )
        else:
            attachments.append(
                parse_single_file(
                    file_path, vision_budget, stage_callback, recognition_mode, vision_report
                )
            )

    return SkillV3InputPayload(
        mail_subject=final_subject,
        mail_body=compress_text_content(final_body, max_chars=10000),
        attachments=attachments[:10],  # Process up to 10 key attachments
    )
