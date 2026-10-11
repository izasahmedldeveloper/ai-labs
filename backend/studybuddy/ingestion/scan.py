"""Security checks on uploads (LLD 04 s2): malware hook and poisoned-document scan."""
from __future__ import annotations

from typing import Protocol

from ..guardrails.policy import Policy
from ..guardrails.text import fold_fullwidth, skeleton, squash, strip_invisible
from .types import ParsedDocument


class MalwareScanner(Protocol):
    async def is_clean(self, data: bytes) -> bool: ...


class AcceptAllScanner:
    """PLACEHOLDER. It scans nothing. A real scanner (e.g. ClamAV) must replace it before production; the README says so."""

    async def is_clean(self, data: bytes) -> bool:
        return True


def injection_findings(text: str, policy: Policy | None = None) -> list[str]:
    """Rule ids of injection phrases (any rule mode: a document is flagged for review even where the live rule is only
    in shadow) plus 'hidden_text' for invisible tag characters."""
    policy = policy or Policy()
    folded = fold_fullwidth(text)
    _, _, tag_chars = strip_invisible(folded)
    found = ["hidden_text"] if tag_chars else []
    skel = skeleton(text)
    views = {"raw": text, "skeleton": skel, "squashed": squash(skel)}
    for rule in policy.input_rules:
        if rule.category == "injection" and rule.pattern.search(views[rule.target]):
            found.append(rule.id)
    return found


def scan_document(doc: ParsedDocument, policy: Policy | None = None) -> dict[int, list[str]]:
    """block index -> findings."""
    out = {}
    for i, b in enumerate(doc.blocks):
        f = injection_findings(b.text, policy)
        if f:
            out[i] = f
    return out
