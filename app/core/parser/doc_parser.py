import io
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Tuple

try:
    import olefile
except ImportError:
    olefile = None

logger = logging.getLogger(__name__)


def _extract_rtf_text(data: bytes) -> str:
    """Extracts plain text from RTF formatted content."""
    try:
        text = data.decode("latin1", errors="replace")
        # Remove RTF control sequences like \par, \b, \f0, \pard, etc.
        text = re.sub(r"\\[a-zA-Z]+(-?\d+)? ?", " ", text)
        # Remove group brackets
        text = re.sub(r"[{}\\]", " ", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Error parsing RTF text: {e}")
        return ""


def _extract_xml_html_text(data: bytes) -> str:
    """Extracts plain text from XML or HTML formatted content."""
    try:
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Error parsing XML/HTML text: {e}")
        return ""


def _extract_ole_word_text(data: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Extracts text and table-like structure from a binary OLE2 WordDocument stream.
    """
    if olefile is None:
        logger.warning("olefile is not installed, falling back to raw text extraction")
        return _extract_raw_text(data), []

    extracted_chunks = []
    tables = []

    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            if ole.exists("WordDocument"):
                word_stream = ole.openstream("WordDocument").read()
                
                # 1. Extract UTF-16LE text sequences (standard in Word 97-2003)
                # Word 97-2003 text is UTF-16LE encoded.
                for m in re.finditer(rb'((?:[\x20-\x7E\r\n\t]\x00)+)', word_stream):
                    try:
                        decoded = m.group(0).decode("utf-16le", errors="ignore").strip()
                        if len(decoded) > 1:
                            extracted_chunks.append(decoded)
                    except Exception:
                        pass

                # 2. Extract Multilingual / Chinese UTF-16LE runs
                for m in re.finditer(rb'((?:[\x00-\xFF][\x00-\xFF]){4,})', word_stream):
                    raw_chunk = m.group(0)
                    try:
                        decoded = raw_chunk.decode("utf-16le", errors="ignore")
                        cleaned = "".join(c for c in decoded if c.isprintable() or c in "\r\n\t ")
                        if len(cleaned.strip()) >= 3 and any("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in cleaned):
                            if cleaned.strip() not in extracted_chunks:
                                extracted_chunks.append(cleaned.strip())
                    except Exception:
                        pass

                # 3. Extract 8-bit ASCII / UTF-8 / GBK runs
                for m in re.finditer(rb'([\x20-\x7E\r\n\t]{4,})', word_stream):
                    try:
                        decoded = m.group(0).decode("utf-8", errors="ignore").strip()
                        if len(decoded) > 3 and decoded not in extracted_chunks:
                            extracted_chunks.append(decoded)
                    except Exception:
                        pass

    except Exception as e:
        logger.warning(f"Error reading OLE2 WordDocument stream: {e}")

    if not extracted_chunks:
        # Fallback to raw text extraction from binary payload
        return _extract_raw_text(data), []

    # Clean, deduplicate sequential duplicates, and construct structured text
    unique_lines = []
    for chunk in extracted_chunks:
        for line in chunk.splitlines():
            s = line.strip()
            if s and (not unique_lines or unique_lines[-1] != s):
                unique_lines.append(s)

    return "\n".join(unique_lines), tables


def _extract_raw_text(data: bytes) -> str:
    """Extracts printable ASCII, UTF-8, and UTF-16LE sequences from arbitrary binary data."""
    extracted = []
    
    # 1. UTF-16LE text sequences (common in Word binary documents)
    for m in re.finditer(rb'((?:[\x20-\x7E\r\n\t]\x00)+)', data):
        try:
            s = m.group(0).decode("utf-16le", errors="ignore").strip()
            if len(s) > 2:
                extracted.append(s)
        except Exception:
            pass

    # 2. ASCII / UTF-8 text sequences
    matches = re.findall(rb'[\x20-\x7E\r\n\t]{4,}', data)
    for m in matches:
        s = m.decode("latin1", errors="ignore").strip()
        if len(s) > 3 and s not in extracted:
            extracted.append(s)

    return "\n".join(extracted)


def parse_doc(file_path: Path) -> Tuple[str, List[Any], str]:
    """
    Parses a legacy Word (.doc) document.
    
    Supports:
    - Standard Word 97-2003 binary OLE2 files (.doc)
    - Modern Word XML files (.docx / ZIP) misnamed with .doc extension
    - Rich Text Format (.rtf) misnamed with .doc extension
    - HTML / XML documents misnamed with .doc extension

    Returns:
        (text, tables, ocr_text)
    """
    try:
        data = file_path.read_bytes()
        if not data:
            return "", [], ""

        # 1. Check if the file is actually a DOCX (ZIP archive signature PK\x03\x04)
        if data.startswith(b"PK\x03\x04"):
            from app.core.parser.word_parser import parse_word
            return parse_word(file_path)

        # 2. Check if the file is Rich Text Format (RTF signature {\rtf)
        if data.startswith(b"{\\rtf"):
            rtf_text = _extract_rtf_text(data)
            return rtf_text, [], ""

        # 3. Check if the file is HTML or XML format
        stripped_prefix = data[:256].strip().lower()
        if stripped_prefix.startswith((b"<?xml", b"<html", b"<!doctype", b"<table")):
            xml_text = _extract_xml_html_text(data)
            return xml_text, [], ""

        # 4. Binary OLE2 Compound Document
        if olefile is not None and olefile.isOleFile(io.BytesIO(data)):
            text, tables = _extract_ole_word_text(data)
            return text, tables, ""

        # 5. Generic binary text extraction fallback
        raw_text = _extract_raw_text(data)
        return raw_text, [], ""

    except Exception as e:
        logger.error(f"Failed to parse .doc document {file_path}: {e}")
        return "", [], f"Error parsing Word .doc: {str(e)}"
