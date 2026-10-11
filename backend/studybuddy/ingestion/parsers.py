"""File-type detection and parsing (LLD 04 s2). Uploads are untrusted: type is decided from the BYTES, not the name.

Parsing of DOCX/PPTX uses the standard library only (zip + XML with a size cap); PDF uses pypdf when installed.
Parsers must run in a restricted worker process (not built here; see README).
"""
from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree as ET

from .types import Block, IngestionError, Limits, ParsedDocument, REASONS

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
EXT_TO_TYPE = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".md": "text", ".markdown": "text", ".txt": "text"}


def _fail(code: str) -> IngestionError:
    return IngestionError(code, REASONS[code])


def sniff(data: bytes, filename: str) -> str:
    """Real file type from content; raises if unsupported or if it contradicts the extension."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    claimed = EXT_TO_TYPE.get(ext)
    if claimed is None:
        raise _fail("unsupported")
    if data[:4] == b"%PDF":
        actual = "pdf"
    elif data[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        raise _fail("encrypted" if claimed in ("docx", "pptx") else "type_mismatch")  # encrypted OOXML is an OLE container
    elif data[:2] == b"PK":
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            raise _fail("corrupt") from None
        actual = "docx" if "word/document.xml" in names else "pptx" if any(n.startswith("ppt/slides/") for n in names) else "zip"
    else:
        actual = "text" if b"\x00" not in data[:4096] else "binary"
    if actual in ("zip", "binary"):
        raise _fail("unsupported")
    if actual != claimed:
        raise _fail("type_mismatch")
    return actual


def _read_member(z: zipfile.ZipFile, name: str, limits: Limits) -> bytes:
    info = z.getinfo(name)
    if info.file_size > limits.max_unzipped_bytes:
        raise _fail("too_large")
    return z.read(name)


def _check_zip(z: zipfile.ZipFile, limits: Limits) -> None:
    if sum(i.file_size for i in z.infolist()) > limits.max_unzipped_bytes:
        raise _fail("too_large")  # zip bomb


def _xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data[:2000] or b"<!ENTITY" in data[:5000]:
        raise _fail("corrupt")  # no DTDs/entities: blocks entity-expansion attacks
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise _fail("corrupt") from None


# ----------------------------------------------------------------------------------------------- text / markdown
_FENCE = re.compile(r"^(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def parse_text(data: bytes) -> ParsedDocument:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _fail("corrupt") from None
    doc, para, code, fence, table = ParsedDocument(file_type="text"), [], [], None, []

    def flush_para():
        if para:
            doc.blocks.append(Block("paragraph", " ".join(para).strip()))
            para.clear()

    def flush_table():
        if table:
            doc.blocks.append(Block("table", "\n".join(table)))
            table.clear()

    for line in text.splitlines():
        if fence:
            code.append(line)
            if line.strip().startswith(fence):
                doc.blocks.append(Block("code", "\n".join(code)))
                code.clear()
                fence = None
            continue
        m = _FENCE.match(line.strip())
        if m:
            flush_para(); flush_table()
            fence, code = m.group(1), [line]
            continue
        if line.strip().startswith("|"):
            flush_para()
            table.append(line.strip())
            continue
        flush_table()
        h = _HEADING.match(line)
        if h:
            flush_para()
            doc.blocks.append(Block("heading", h.group(2), level=len(h.group(1))))
        elif not line.strip():
            flush_para()
        else:
            para.append(line.strip())
    if code:  # unterminated fence: keep it whole rather than lose it
        doc.blocks.append(Block("code", "\n".join(code)))
    flush_para(); flush_table()
    return doc


# ----------------------------------------------------------------------------------------------- docx
def _wtext(el: ET.Element) -> str:
    out = []
    for node in el.iter():
        if node.tag == f"{W}t" and node.text:
            out.append(node.text)
        elif node.tag in (f"{W}tab",):
            out.append(" ")
        elif node.tag in (f"{W}br", f"{W}cr"):
            out.append("\n")
    return "".join(out).strip()


def parse_docx(data: bytes, limits: Limits) -> ParsedDocument:
    try:
        z = zipfile.ZipFile(BytesIO(data))
        _check_zip(z, limits)
        root = _xml(_read_member(z, "word/document.xml", limits))
    except (zipfile.BadZipFile, KeyError):
        raise _fail("corrupt") from None
    body = root.find(f"{W}body")
    if body is None:
        raise _fail("corrupt")
    doc = ParsedDocument(file_type="docx")
    for child in body:
        if child.tag == f"{W}p":
            text = _wtext(child)
            if not text:
                continue
            style = child.find(f"{W}pPr/{W}pStyle")
            sid = (style.get(f"{W}val") if style is not None else "") or ""
            m = re.match(r"(?i)heading\s*(\d)", sid)
            if m:
                doc.blocks.append(Block("heading", text, level=int(m.group(1))))
            elif sid.lower() == "title":
                doc.blocks.append(Block("heading", text, level=1))
            else:
                doc.blocks.append(Block("paragraph", text))
        elif child.tag == f"{W}tbl":
            rows = [" | ".join(_wtext(c) for c in tr.findall(f"{W}tc")) for tr in child.findall(f"{W}tr")]
            rows = [r for r in rows if r.strip(" |")]
            if rows:
                doc.blocks.append(Block("table", "\n".join(rows)))
    return doc


# ----------------------------------------------------------------------------------------------- pptx
def _slide_no(name: str) -> int:
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _a_paragraphs(root: ET.Element) -> list[str]:
    paras = []
    for p in root.iter(f"{A}p"):
        t = "".join(n.text or "" for n in p.iter(f"{A}t")).strip()
        if t:
            paras.append(t)
    return paras


def parse_pptx(data: bytes, limits: Limits) -> ParsedDocument:
    try:
        z = zipfile.ZipFile(BytesIO(data))
        _check_zip(z, limits)
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_slide_no)
        if len(slides) > limits.max_pages:
            raise _fail("too_many_pages")
        doc = ParsedDocument(file_type="pptx", pages=len(slides))
        for name in slides:
            no = _slide_no(name)
            paras = _a_paragraphs(_xml(_read_member(z, name, limits)))
            notes_name = f"ppt/notesSlides/notesSlide{no}.xml"  # speaker notes are part of the slide (LLD 04 s2)
            notes = _a_paragraphs(_xml(_read_member(z, notes_name, limits))) if notes_name in z.namelist() else []
            notes = [n for n in notes if not n.isdigit()]  # slide-number placeholder
            if not paras and not notes:
                continue
            title = paras[0] if paras else None
            text = "\n".join(paras[1:] if title else paras)
            if notes:
                text += ("\n" if text else "") + "Notes: " + " ".join(notes)
            doc.blocks.append(Block("slide", text.strip(), page=no, title=title))
    except (zipfile.BadZipFile, KeyError):
        raise _fail("corrupt") from None
    return doc


# ----------------------------------------------------------------------------------------------- pdf
def _pdf_blocks(page_text: str, page: int) -> list[Block]:
    blocks, para = [], []
    lines = [ln.rstrip() for ln in page_text.splitlines()]

    def flush():
        if para:
            blocks.append(Block("paragraph", " ".join(para), page=page))
            para.clear()

    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            flush()
            continue
        prev_blank = i == 0 or not lines[i - 1].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # Heuristic only: PDFs carry no heading structure. A short line without end punctuation, alone before text.
        if prev_blank and nxt and len(s) <= 70 and not s.endswith((".", ",", ";", ":")) and len(s.split()) <= 10:
            flush()
            blocks.append(Block("heading", s, level=2, page=page))
        else:
            para.append(s)
    flush()
    return blocks


def parse_pdf(data: bytes, limits: Limits) -> ParsedDocument:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise IngestionError("unsupported", "PDF support is not installed on this server.") from None
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise _fail("encrypted")
        n = len(reader.pages)
        if n > limits.max_pages:
            raise _fail("too_many_pages")
        doc = ParsedDocument(file_type="pdf", pages=n)
        for i, page in enumerate(reader.pages, start=1):
            doc.blocks.extend(_pdf_blocks(page.extract_text() or "", i))
    except IngestionError:
        raise
    except (PdfReadError, Exception):
        raise _fail("corrupt") from None
    if n and doc.char_count / n < limits.min_chars_per_page:
        raise _fail("scanned" if doc.char_count else "empty")
    return doc


def parse(data: bytes, filename: str, limits: Limits | None = None) -> ParsedDocument:
    limits = limits or Limits()
    if len(data) > limits.max_bytes:
        raise _fail("too_large")
    if not data:
        raise _fail("empty")
    kind = sniff(data, filename)
    doc = {"pdf": lambda: parse_pdf(data, limits), "docx": lambda: parse_docx(data, limits),
           "pptx": lambda: parse_pptx(data, limits), "text": lambda: parse_text(data)}[kind]()
    if doc.pages > limits.max_pages:
        raise _fail("too_many_pages")
    if not doc.blocks or not doc.char_count:
        raise _fail("empty")
    return doc
