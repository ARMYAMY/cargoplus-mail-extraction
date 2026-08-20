import logging
from pathlib import Path
import re
from typing import List
from app.schemas.task import AttachmentInput, SkillV3InputPayload
from app.core.parser.eml_parser import parse_eml
from app.core.parser.pdf_parser import parse_pdf
from app.core.parser.excel_parser import parse_excel
from app.core.parser.word_parser import parse_word
from app.core.parser.doc_parser import parse_doc
from app.core.parser.ocr_engine import extract_ocr_from_image

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

    lines = text.splitlines()
    high_priority_lines = []
    normal_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if CARGO_KEYWORDS.search(stripped):
            high_priority_lines.append(stripped)
        else:
            normal_lines.append(stripped)

    # First take high-priority lines
    compressed = "\n".join(high_priority_lines)
    if len(compressed) < max_chars:
        remaining = max_chars - len(compressed)
        for line in normal_lines:
            if len(line) + 1 <= remaining:
                compressed += "\n" + line
                remaining -= len(line) + 1
            else:
                break

    return compressed[:max_chars]


def parse_single_file(file_path: Path) -> AttachmentInput:
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
            text, tables, ocr_text = parse_pdf(file_path)
        elif ext == ".xlsx":
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            text, tables, ocr_text = parse_excel(file_path)
        elif ext == ".docx":
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            text, tables, ocr_text = parse_word(file_path)
        elif ext == ".doc":
            content_type = "application/msword"
            text, tables, ocr_text = parse_doc(file_path)
        elif ext in IMAGE_EXTENSIONS:
            content_type = f"image/{ext.lstrip('.')}"
            ocr_text = extract_ocr_from_image(file_path)
        elif ext in {".txt", ".csv", ".json", ".md"}:
            content_type = "text/plain"
            text = file_path.read_text(encoding="utf-8", errors="replace")
        else:
            text = f"[Binary or unsupported attachment format: {filename}]"
    except Exception as e:
        logger.error(f"Error parsing file {file_path}: {e}")
        text = f"[Error parsing attachment {filename}: {str(e)}]"

    # Smart compression to prevent LLM context overflow and timeouts
    text = compress_text_content(text, max_chars=8000)
    ocr_text = compress_text_content(ocr_text, max_chars=6000)

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
) -> SkillV3InputPayload:
    """
    Processes uploaded files (which might include an .eml email file and/or various attachments),
    and converts them into a consolidated SkillV3InputPayload.
    """
    final_subject = subject
    final_body = body
    attachments: List[AttachmentInput] = []

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
                attachments.append(parse_single_file(att_p))
        else:
            attachments.append(parse_single_file(file_path))

    return SkillV3InputPayload(
        mail_subject=final_subject,
        mail_body=compress_text_content(final_body, max_chars=10000),
        attachments=attachments[:10],  # Process up to 10 key attachments
    )
