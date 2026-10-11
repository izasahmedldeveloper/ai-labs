"""Structure-aware chunking (LLD 04 s3).

Order of splitting: headings (a chunk never crosses a heading boundary) -> paragraphs -> sentences (only when a
single paragraph is too big). Tables and code are kept whole (a table too big is split by rows with the header
repeated; code is never split mid-block, even if oversize). One chunk per slide with its notes. The heading path
("A > B") is stored and prepended to the text that gets embedded, not to the stored text.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..retrieval.types import estimate_tokens
from .types import Block, ParsedDocument


@dataclass(frozen=True)
class ChunkConfig:
    target_tokens: int = 400   # D3, to be tuned by measurement
    max_tokens: int = 500
    overlap_tokens: int = 50


@dataclass(frozen=True)
class DraftChunk:
    ordinal: int
    text: str
    page: int | None
    heading_path: str | None
    content_hash: str
    token_count: int

    @property
    def embedding_text(self) -> str:
        return f"{self.heading_path}\n{self.text}" if self.heading_path else self.text


def content_hash(text: str, heading_path: str | None) -> str:
    return hashlib.sha256(f"{heading_path or ''}\x1f{text}".encode()).hexdigest()


_SENT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str, limit: int) -> list[str]:
    parts, cur = [], []
    for s in _SENT.split(text):
        words = s.split()
        while len(words) * 1.3 > limit:  # a single sentence over the limit: hard split by words
            k = max(1, int(limit / 1.3))
            if cur:
                parts.append(" ".join(cur)); cur = []
            parts.append(" ".join(words[:k])); words = words[k:]
        if not words:
            continue
        s = " ".join(words)
        if cur and estimate_tokens(" ".join(cur + [s])) > limit:
            parts.append(" ".join(cur)); cur = []
        cur.append(s)
    if cur:
        parts.append(" ".join(cur))
    return parts


def _tail(text: str, tokens: int) -> str:
    words = text.split()
    k = max(0, int(tokens / 1.3))
    return " ".join(words[-k:]) if k else ""


def _split_table(text: str, cfg: ChunkConfig) -> list[str]:
    if estimate_tokens(text) <= cfg.max_tokens:
        return [text]
    rows = text.split("\n")
    header = rows[:1]
    out, cur = [], []
    for row in rows[1:]:
        if cur and estimate_tokens("\n".join(header + cur + [row])) > cfg.max_tokens:
            out.append("\n".join(header + cur)); cur = []
        cur.append(row)
    if cur:
        out.append("\n".join(header + cur))
    return out


def chunk_document(doc: ParsedDocument, cfg: ChunkConfig | None = None) -> list[DraftChunk]:
    cfg = cfg or ChunkConfig()
    drafts: list[tuple[str, int | None, str | None]] = []  # text, page, heading path
    path: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_page: int | None = None
    carry = ""  # overlap from the previous chunk, within the same section only

    def hp() -> str | None:
        return " > ".join(t for _, t in path) or None

    def flush(keep_overlap: bool):
        nonlocal buf, buf_page, carry
        if buf:
            text = "\n\n".join(buf)
            drafts.append((text, buf_page, hp()))
            carry = _tail(text, cfg.overlap_tokens) if keep_overlap else ""
        buf, buf_page = [], None

    def add_text(text: str, page: int | None):
        nonlocal buf_page, carry
        pieces = [text] if estimate_tokens(text) <= cfg.max_tokens else _split_sentences(text, cfg.target_tokens)
        for piece in pieces:
            if buf and estimate_tokens("\n\n".join(buf + [piece])) > cfg.target_tokens:
                flush(True)
            if not buf and carry:
                buf.append(carry)
                carry = ""
            if buf_page is None:
                buf_page = page
            buf.append(piece)

    for b in doc.blocks:
        if b.kind == "heading":
            flush(False)
            while path and path[-1][0] >= b.level:
                path.pop()
            path.append((b.level, b.text))
        elif b.kind == "slide":
            flush(False)
            saved = list(path)
            if b.title:
                path.append((99, b.title))
            drafts.append((b.text or (b.title or ""), b.page, hp()))
            path[:] = saved
        elif b.kind in ("table", "code"):
            flush(False)
            for part in (_split_table(b.text, cfg) if b.kind == "table" else [b.text]):  # code: whole, even if oversize
                drafts.append((part, b.page, hp()))
        else:
            add_text(b.text, b.page)
    flush(False)
    out = []
    for text, page, heading in drafts:
        text = text.strip()
        if text:
            out.append(DraftChunk(len(out), text, page, heading, content_hash(text, heading), estimate_tokens(text)))
    return out
