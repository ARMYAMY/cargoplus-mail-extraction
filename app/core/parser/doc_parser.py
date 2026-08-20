import codecs
from html.parser import HTMLParser
from pathlib import Path
import struct
from typing import Any, List, Tuple

from app.config import settings

try:
    import olefile
except ImportError:  # pragma: no cover - the production lock file installs it
    olefile = None

WORD97_FIB_IDENT = 0xA5EC
WORD97_MIN_NFIB = 0x00C1
FIB_F_ENCRYPTED = 0x0100
FIB_F_WHICH_TABLE_STREAM = 0x0200
FIB_F_EXT_CHAR = 0x1000
FIB_F_OBFUSCATED = 0x8000
FIB_FC_LCB_CLX_INDEX = 33
MAX_EXTRACTED_CHARS = 100_000
MAX_PIECES = 100_000
MAX_RTF_GROUP_DEPTH = 1_000


class DocParseError(ValueError):
    """Raised when a legacy Word document cannot be parsed safely."""


def _read_u16(data: bytes, offset: int, field_name: str) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise DocParseError(f"Invalid DOC structure: missing {field_name}")
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data: bytes, offset: int, field_name: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise DocParseError(f"Invalid DOC structure: missing {field_name}")
    return struct.unpack_from("<I", data, offset)[0]


def _codec_from_fib(chs: int, lid: int) -> str:
    charset_codecs = {
        0x80: "cp932",
        0x81: "cp949",
        0x86: "cp936",
        0x88: "cp950",
        0xA1: "cp1253",
        0xA2: "cp1254",
        0xB1: "cp1255",
        0xB2: "cp1256",
        0xBA: "cp1257",
        0xCC: "cp1251",
        0xDE: "cp874",
        0xEE: "cp1250",
    }
    language_codecs = {
        0x0404: "cp950",
        0x0804: "cp936",
        0x0C04: "cp950",
        0x1004: "cp936",
        0x1404: "cp950",
        0x0411: "cp932",
        0x0412: "cp949",
        0x0419: "cp1251",
    }
    return charset_codecs.get(chs) or language_codecs.get(lid) or "cp1252"


def _clean_word_text(text: str) -> str:
    replacements = {
        "\x07": "\t",
        "\x0b": "\n",
        "\x0c": "\n",
        "\x0d": "\n",
        "\x13": "",
        "\x14": "",
        "\x15": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 0x20)
    lines = [
        "\t".join(" ".join(cell.split()) for cell in line.split("\t")).strip(" \t")
        for line in text.splitlines()
    ]
    return "\n".join(line for line in lines if line).strip()[:MAX_EXTRACTED_CHARS]


def _extract_table_data(text: str) -> List[Any]:
    tables: List[Any] = []
    current_rows: List[List[str]] = []

    def finish_table() -> None:
        if current_rows and len(tables) < 20:
            tables.append({"table_index": len(tables), "rows": list(current_rows[:200])})
        current_rows.clear()

    for line in text.splitlines():
        if "\t" not in line:
            finish_table()
            continue
        cells = [cell.strip()[:2_000] for cell in line.split("\t")[:100]]
        while cells and not cells[-1]:
            cells.pop()
        if len(cells) >= 2 and any(cells):
            current_rows.append(cells)
        else:
            finish_table()
    finish_table()
    return tables


def _parse_fib(
    word_stream: bytes,
) -> Tuple[str, int, int, int, int, bool, int, int, int]:
    if _read_u16(word_stream, 0, "wIdent") != WORD97_FIB_IDENT:
        raise DocParseError("The OLE file does not contain a supported Word document")

    n_fib = _read_u16(word_stream, 2, "nFib")
    if n_fib < WORD97_MIN_NFIB:
        raise DocParseError("Only Word 97-2003 binary documents are supported")

    lid = _read_u16(word_stream, 6, "lid")
    flags = _read_u16(word_stream, 10, "FibBase flags")
    if flags & (FIB_F_ENCRYPTED | FIB_F_OBFUSCATED):
        raise DocParseError("Encrypted or obfuscated Word documents are not supported")

    chs = _read_u16(word_stream, 20, "chs")
    fc_min = _read_u32(word_stream, 24, "fcMin")
    fc_mac = _read_u32(word_stream, 28, "fcMac")

    csw = _read_u16(word_stream, 32, "csw")
    fib_rg_lw_offset = 34 + csw * 2
    cslw = _read_u16(word_stream, fib_rg_lw_offset, "cslw")
    fib_rg_lw_values = fib_rg_lw_offset + 2
    if cslw < 4:
        raise DocParseError("Invalid DOC structure: FibRgLw is incomplete")
    ccp_text = _read_u32(word_stream, fib_rg_lw_values + 3 * 4, "ccpText")

    fib_rg_fc_lcb_offset = fib_rg_lw_values + cslw * 4
    cb_rg_fc_lcb = _read_u16(word_stream, fib_rg_fc_lcb_offset, "cbRgFcLcb")
    if cb_rg_fc_lcb <= FIB_FC_LCB_CLX_INDEX:
        raise DocParseError("Invalid DOC structure: fcClx is missing")
    fc_lcb_values = fib_rg_fc_lcb_offset + 2
    required_size = fc_lcb_values + cb_rg_fc_lcb * 8
    if required_size > len(word_stream):
        raise DocParseError("Invalid DOC structure: FibRgFcLcb exceeds WordDocument")

    clx_offset = fc_lcb_values + FIB_FC_LCB_CLX_INDEX * 8
    fc_clx = _read_u32(word_stream, clx_offset, "fcClx")
    lcb_clx = _read_u32(word_stream, clx_offset + 4, "lcbClx")
    table_name = "1Table" if flags & FIB_F_WHICH_TABLE_STREAM else "0Table"
    return (
        table_name,
        fc_clx,
        lcb_clx,
        ccp_text,
        fc_min,
        bool(flags & FIB_F_EXT_CHAR),
        fc_mac,
        chs,
        lid,
    )


def _decode_piece(raw: bytes, compressed: bool, codec: str) -> str:
    if compressed:
        return raw.decode(codec, errors="replace")
    if len(raw) % 2:
        raise DocParseError("Invalid DOC structure: truncated UTF-16 text piece")
    return raw.decode("utf-16le", errors="strict")


def _extract_piece_table_text(
    word_stream: bytes,
    table_stream: bytes,
    fc_clx: int,
    lcb_clx: int,
    ccp_text: int,
    codec: str,
) -> str:
    if lcb_clx <= 0 or fc_clx < 0 or fc_clx + lcb_clx > len(table_stream):
        raise DocParseError("Invalid DOC structure: CLX is outside the table stream")

    clx = table_stream[fc_clx:fc_clx + lcb_clx]
    offset = 0
    plc_pcd = None
    while offset < len(clx):
        marker = clx[offset]
        if marker == 0x01:
            grpprl_size = _read_u16(clx, offset + 1, "cbGrpprl")
            offset += 3 + grpprl_size
            if offset > len(clx):
                raise DocParseError("Invalid DOC structure: truncated Prc in CLX")
        elif marker == 0x02:
            pcdt_size = _read_u32(clx, offset + 1, "lcb")
            pcdt_start = offset + 5
            pcdt_end = pcdt_start + pcdt_size
            if pcdt_end > len(clx):
                raise DocParseError("Invalid DOC structure: truncated Pcdt in CLX")
            plc_pcd = clx[pcdt_start:pcdt_end]
            break
        else:
            raise DocParseError("Invalid DOC structure: unknown CLX record")

    if plc_pcd is None or len(plc_pcd) < 4 or (len(plc_pcd) - 4) % 12:
        raise DocParseError("Invalid DOC structure: malformed PlcPcd")

    piece_count = (len(plc_pcd) - 4) // 12
    if piece_count <= 0 or piece_count > MAX_PIECES:
        raise DocParseError("Invalid DOC structure: unreasonable text piece count")

    cp_count = piece_count + 1
    pcd_offset = cp_count * 4
    codepoints = [
        _read_u32(plc_pcd, index * 4, f"aCP[{index}]")
        for index in range(cp_count)
    ]
    if any(end < start for start, end in zip(codepoints, codepoints[1:])):
        raise DocParseError("Invalid DOC structure: text piece positions are not ordered")

    document_end = ccp_text if ccp_text > 0 else codepoints[-1]
    chunks = []
    extracted_chars = 0
    for index in range(piece_count):
        cp_start = codepoints[index]
        cp_end = min(codepoints[index + 1], document_end)
        if cp_end <= cp_start or extracted_chars >= MAX_EXTRACTED_CHARS:
            continue

        pcd = pcd_offset + index * 8
        fc_compressed = _read_u32(plc_pcd, pcd + 2, f"Pcd[{index}].fc")
        compressed = bool(fc_compressed & 0x40000000)
        file_offset = fc_compressed & 0x3FFFFFFF
        if compressed:
            file_offset //= 2

        chars_to_read = min(cp_end - cp_start, MAX_EXTRACTED_CHARS - extracted_chars)
        byte_width = 1 if compressed else 2
        piece_start = file_offset
        piece_end = piece_start + chars_to_read * byte_width
        if piece_start < 0 or piece_end > len(word_stream):
            raise DocParseError("Invalid DOC structure: text piece exceeds WordDocument")

        chunks.append(_decode_piece(word_stream[piece_start:piece_end], compressed, codec))
        extracted_chars += chars_to_read

    return _clean_word_text("".join(chunks))


def _extract_word_stream_text(word_stream: bytes, table_stream: bytes) -> str:
    (
        _table_name,
        fc_clx,
        lcb_clx,
        ccp_text,
        fc_min,
        is_unicode,
        fc_mac,
        chs,
        lid,
    ) = _parse_fib(word_stream)
    codec = _codec_from_fib(chs, lid)

    if lcb_clx:
        text = _extract_piece_table_text(
            word_stream,
            table_stream,
            fc_clx,
            lcb_clx,
            ccp_text,
            codec,
        )
    else:
        if fc_min > fc_mac or fc_mac > len(word_stream):
            raise DocParseError("Invalid DOC structure: simple text range is invalid")
        raw = word_stream[fc_min:fc_mac]
        text = _clean_word_text(_decode_piece(raw, not is_unicode, codec))

    if not text:
        raise DocParseError("The Word document contains no extractable text")
    return text


def _extract_ole_word_text(file_path: Path) -> Tuple[str, List[Any]]:
    if olefile is None:
        raise DocParseError("olefile is required to parse Word 97-2003 documents")

    try:
        with olefile.OleFileIO(str(file_path)) as ole:
            if not ole.exists("WordDocument"):
                raise DocParseError("The OLE file does not contain a WordDocument stream")
            word_size = ole.get_size("WordDocument")
            if word_size > settings.MAX_LEGACY_DOC_FILE_SIZE:
                raise DocParseError("The WordDocument stream exceeds the legacy DOC size limit")
            word_stream = ole.openstream("WordDocument").read()
            table_name, _, lcb_clx, *_ = _parse_fib(word_stream)
            if lcb_clx:
                if not ole.exists(table_name):
                    raise DocParseError(f"The OLE file does not contain the required {table_name} stream")
                table_size = ole.get_size(table_name)
                if table_size > settings.MAX_LEGACY_DOC_FILE_SIZE:
                    raise DocParseError("The Word table stream exceeds the legacy DOC size limit")
                table_stream = ole.openstream(table_name).read()
            else:
                table_stream = b""
    except DocParseError:
        raise
    except Exception as exc:
        raise DocParseError(f"Invalid Word 97-2003 OLE document: {exc}") from exc

    text = _extract_word_stream_text(word_stream, table_stream)
    return text, _extract_table_data(text)


_RTF_DESTINATIONS = {
    "author", "category", "colortbl", "comment", "company", "creatim", "doccomm",
    "filetbl", "fonttbl", "generator", "info", "keywords", "operator", "pict",
    "printim", "private", "revtim", "stylesheet", "subject", "title", "xmlnstbl",
    "object", "objdata", "datastore", "themedata",
}
_RTF_SPECIAL_WORDS = {
    "bullet": "•", "cell": "\t", "emdash": "—", "emspace": "\u2003",
    "endash": "–", "enspace": "\u2002", "line": "\n", "lquote": "‘",
    "page": "\n", "par": "\n", "qmspace": "\u2005", "row": "\n",
    "rquote": "’", "tab": "\t",
}
_RTF_CODEPAGES = {"ansi": "cp1252", "mac": "mac_roman", "pc": "cp437", "pca": "cp850"}


def _extract_rtf_text(data: bytes) -> str:
    source = data.decode("latin1", errors="strict")
    output: List[str] = []
    output_length = 0
    ansi_bytes = bytearray()
    stack: List[Tuple[bool, int, str, bool]] = []
    skip_destination = False
    unicode_fallback_count = 1
    codepage = "cp1252"
    ignorable_destination = False
    skip_fallback_chars = 0

    def append_output(value: str) -> None:
        nonlocal output_length
        if skip_destination or not value or output_length >= MAX_EXTRACTED_CHARS:
            return
        remaining = MAX_EXTRACTED_CHARS - output_length
        value = value[:remaining]
        output.append(value)
        output_length += len(value)

    def flush_ansi() -> None:
        if not ansi_bytes:
            return
        raw = bytes(ansi_bytes)
        ansi_bytes.clear()
        try:
            append_output(raw.decode(codepage, errors="replace"))
        except LookupError:
            append_output(raw.decode("cp1252", errors="replace"))

    index = 0
    while index < len(source) and output_length < MAX_EXTRACTED_CHARS:
        char = source[index]
        if char == "{":
            flush_ansi()
            if len(stack) >= MAX_RTF_GROUP_DEPTH:
                raise DocParseError("RTF group nesting exceeds the safety limit")
            stack.append((skip_destination, unicode_fallback_count, codepage, ignorable_destination))
            ignorable_destination = False
            index += 1
            continue
        if char == "}":
            flush_ansi()
            if not stack:
                raise DocParseError("Malformed RTF: unmatched closing group")
            skip_destination, unicode_fallback_count, codepage, ignorable_destination = stack.pop()
            index += 1
            continue
        if char != "\\":
            if skip_fallback_chars:
                skip_fallback_chars -= 1
            elif not skip_destination and char not in "\r\n":
                ansi_bytes.append(ord(char))
                if len(ansi_bytes) >= 64 * 1024:
                    flush_ansi()
            index += 1
            continue

        if index + 1 >= len(source):
            raise DocParseError("Malformed RTF: trailing escape")
        next_char = source[index + 1]
        if next_char in "{}\\":
            if skip_fallback_chars:
                skip_fallback_chars -= 1
            elif not skip_destination:
                ansi_bytes.append(ord(next_char))
            index += 2
            continue
        if next_char == "'":
            if index + 3 >= len(source):
                raise DocParseError("Malformed RTF: truncated hexadecimal escape")
            try:
                byte_value = int(source[index + 2:index + 4], 16)
            except ValueError as exc:
                raise DocParseError("Malformed RTF: invalid hexadecimal escape") from exc
            if skip_fallback_chars:
                skip_fallback_chars -= 1
            elif not skip_destination:
                ansi_bytes.append(byte_value)
            index += 4
            continue
        if next_char == "*":
            flush_ansi()
            ignorable_destination = True
            index += 2
            continue
        if not next_char.isalpha():
            flush_ansi()
            symbols = {"~": "\u00a0", "-": "\u00ad", "_": "\u2011"}
            if skip_fallback_chars:
                skip_fallback_chars -= 1
            elif not skip_destination:
                append_output(symbols.get(next_char, ""))
            index += 2
            continue

        flush_ansi()
        word_start = index + 1
        word_end = word_start
        while word_end < len(source) and source[word_end].isalpha():
            word_end += 1
        control_word = source[word_start:word_end]
        number_start = word_end
        if number_start < len(source) and source[number_start] in "+-":
            number_start += 1
        number_end = number_start
        while number_end < len(source) and source[number_end].isdigit():
            number_end += 1
        has_number = number_end > number_start
        number_text = source[word_end:number_end] if has_number else ""
        parameter = int(number_text) if number_text else None
        index = number_end
        if index < len(source) and source[index] == " ":
            index += 1

        if control_word in _RTF_DESTINATIONS or ignorable_destination:
            skip_destination = True
            ignorable_destination = False
            continue
        if control_word in _RTF_CODEPAGES:
            codepage = _RTF_CODEPAGES[control_word]
            continue
        if control_word == "ansicpg" and parameter is not None:
            candidate = f"cp{parameter}"
            try:
                codecs.lookup(candidate)
                codepage = candidate
            except LookupError:
                codepage = "cp1252"
            continue
        if control_word == "uc" and parameter is not None:
            unicode_fallback_count = max(0, min(parameter, 16))
            continue
        if control_word == "u" and parameter is not None:
            if parameter < 0:
                parameter += 65536
            append_output(chr(parameter))
            skip_fallback_chars = unicode_fallback_count
            continue
        if control_word == "bin" and parameter is not None:
            if parameter < 0 or index + parameter > len(source):
                raise DocParseError("Malformed RTF: invalid binary payload length")
            index += parameter
            continue
        if control_word in _RTF_SPECIAL_WORDS:
            if skip_fallback_chars:
                skip_fallback_chars -= 1
            else:
                append_output(_RTF_SPECIAL_WORDS[control_word])

    truncated_at_limit = index < len(source) and output_length >= MAX_EXTRACTED_CHARS
    flush_ansi()
    if stack and not truncated_at_limit:
        raise DocParseError("Malformed RTF: unclosed group")
    text = _clean_word_text("".join(output))
    if not text:
        raise DocParseError("The RTF document contains no extractable text")
    return text


class _BoundedHTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.length = 0
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1
        elif tag.lower() in {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append("\n")
        elif tag.lower() in {"td", "th"}:
            self._append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag.lower() in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self._append(data)

    def _append(self, value: str) -> None:
        if self.length >= MAX_EXTRACTED_CHARS:
            return
        value = value[:MAX_EXTRACTED_CHARS - self.length]
        self.parts.append(value)
        self.length += len(value)


def _extract_xml_html_text(data: bytes) -> str:
    parser = _BoundedHTMLTextParser()
    try:
        parser.feed(data.decode("utf-8-sig", errors="replace"))
        parser.close()
    except Exception as exc:
        raise DocParseError(f"Malformed Word HTML/XML document: {exc}") from exc
    text = _clean_word_text("".join(parser.parts))
    if not text:
        raise DocParseError("The Word HTML/XML document contains no extractable text")
    return text


def parse_doc(file_path: Path, vision_budget=None) -> Tuple[str, List[Any], str]:
    """Parse a legacy Word 97-2003, RTF, or Word HTML document safely."""
    try:
        file_size = file_path.stat().st_size
    except OSError as exc:
        raise DocParseError(f"Unable to read Word document: {exc}") from exc
    if file_size <= 0:
        raise DocParseError("The Word document is empty")
    if file_size > settings.MAX_LEGACY_DOC_FILE_SIZE:
        raise DocParseError("The Word document exceeds the legacy DOC size limit")

    try:
        with file_path.open("rb") as handle:
            prefix = handle.read(512)
    except OSError as exc:
        raise DocParseError(f"Unable to read Word document: {exc}") from exc

    try:
        normalized_prefix = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
        if normalized_prefix.startswith(b"PK\x03\x04"):
            from app.core.parser.word_parser import parse_word

            text, tables, error_text = parse_word(file_path, vision_budget)
            if error_text:
                raise DocParseError(error_text)
            if not text and not tables:
                raise DocParseError("The Word document contains no extractable content")
            return text, tables, ""

        if normalized_prefix.startswith(b"{\\rtf"):
            text = _extract_rtf_text(file_path.read_bytes())
            return text, _extract_table_data(text), ""

        lower_prefix = normalized_prefix.lower()
        if lower_prefix.startswith((b"<?xml", b"<html", b"<!doctype", b"<table")):
            text = _extract_xml_html_text(file_path.read_bytes())
            return text, _extract_table_data(text), ""

        if olefile is not None and olefile.isOleFile(str(file_path)):
            text, tables = _extract_ole_word_text(file_path)
            return text, tables, ""
    except DocParseError:
        raise
    except Exception as exc:
        raise DocParseError(f"Unable to parse the Word document safely: {exc}") from exc

    raise DocParseError(
        "The uploaded .doc file is not a supported Word 97-2003, RTF, HTML, or DOCX document"
    )
