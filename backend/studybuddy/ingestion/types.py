"""Ingestion contracts (LLD 04): parsed structure, errors with plain-language reasons, limits."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 50 * 1024 * 1024       # D2
    max_pages: int = 300                    # D2
    max_unzipped_bytes: int = 200 * 1024 * 1024  # zip-bomb guard for DOCX/PPTX
    min_chars_per_page: int = 25            # below this on average a PDF is treated as scanned (OCR deferred, D1)


class IngestionError(Exception):
    """Permanent failure: retrying cannot help. `message` is plain language for the instructor; `code` is for metrics."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


class TemporaryError(Exception):
    """Transient failure (rate limit, network): the message is retried later (LLD 04 s5 D4)."""


@dataclass(frozen=True)
class Block:
    kind: str                 # heading | paragraph | table | code | slide
    text: str
    level: int = 0            # heading level (1 = top)
    page: int | None = None
    title: str | None = None  # slides: slide title


@dataclass
class ParsedDocument:
    blocks: list[Block] = field(default_factory=list)
    pages: int = 1
    file_type: str = "text"

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks)


REASONS = {
    "too_large": "The file is larger than the 50 MB limit.",
    "too_many_pages": "The file has more than 300 pages.",
    "unsupported": "This file type is not supported. Upload a PDF, Word, PowerPoint, Markdown or text file.",
    "type_mismatch": "The file's contents do not match its extension.",
    "encrypted": "The file is password-protected. Remove the password and upload it again.",
    "corrupt": "The file could not be opened. It may be damaged.",
    "empty": "No text could be found in the file.",
    "scanned": "This looks like a scanned document. Scanned files are not supported yet; upload a version with selectable text.",
    "malware": "The file was rejected by the security scan.",
}
