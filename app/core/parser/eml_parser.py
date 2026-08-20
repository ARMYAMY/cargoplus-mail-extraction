import email
from email import policy
import html
import logging
from pathlib import Path
import re
import uuid
from typing import List, Tuple

logger = logging.getLogger(__name__)

HTML_TAG_RE = re.compile(r'<[^>]+>')
MAX_EML_ATTACHMENTS = 10
MAX_EML_ATTACHMENT_SIZE = 20 * 1024 * 1024
SAFE_ATTACHMENT_EXTENSIONS = {
    ".pdf", ".xlsx", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".bmp",
    ".webp", ".tiff", ".txt", ".csv", ".json", ".md",
}


def html_to_plain_text(html_content: str) -> str:
    """Converts HTML email body to clean readable text."""
    if not html_content:
        return ""
    # Replace <br> and <p> with newlines
    text = re.sub(r'(?i)<br\s*/?>', '\n', html_content)
    text = re.sub(r'(?i)</p>', '\n', text)
    text = re.sub(r'(?i)</tr>', '\n', text)
    text = re.sub(r'(?i)</td>', '\t', text)
    text = HTML_TAG_RE.sub('', text)
    text = html.unescape(text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_eml(file_path: Path, output_dir: Path) -> Tuple[str, str, List[Path]]:
    """
    Parses an .eml email file.
    Returns: (subject, body, extracted_attachment_file_paths)
    """
    subject = ""
    body_text_parts = []
    body_html_parts = []
    attachment_paths = []

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(file_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)

        subject = msg.get("subject", "") or ""

        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition") or "")
            filename = part.get_filename()

            if filename:
                # This is an attachment
                if len(attachment_paths) >= MAX_EML_ATTACHMENTS:
                    continue
                safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', Path(filename).name)[:180]
                if not safe_name or Path(safe_name).suffix.lower() not in SAFE_ATTACHMENT_EXTENSIONS:
                    continue
                payload = part.get_payload(decode=True)
                if payload and len(payload) <= MAX_EML_ATTACHMENT_SIZE:
                    dest_file = output_dir / (
                        f"att_{Path(file_path).stem}_{uuid.uuid4().hex[:12]}_{safe_name}"
                    )
                    dest_file.write_bytes(payload)
                    attachment_paths.append(dest_file)
            elif content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body_text_parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        body_text_parts.append(payload.decode("utf-8", errors="replace"))
            elif content_type == "text/html" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body_html_parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        body_html_parts.append(payload.decode("utf-8", errors="replace"))

        # Consolidate body
        if body_text_parts:
            final_body = "\n\n".join(body_text_parts).strip()
        elif body_html_parts:
            final_body = html_to_plain_text("\n\n".join(body_html_parts))
        else:
            final_body = ""

    except Exception as e:
        logger.error(f"Failed to parse EML file {file_path}: {e}")
        return "", f"Error reading email: {str(e)}", []

    return subject, final_body, attachment_paths
