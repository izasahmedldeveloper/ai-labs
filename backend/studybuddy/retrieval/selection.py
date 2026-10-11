"""Fusion and selection (LLD 05 sections 4 and 6): pure functions, no I/O, easy to test and to tune.

  reciprocal_rank_fusion  combine the vector and keyword rankings using ranks, not incomparable raw scores
  choose                  threshold, de-duplicate, keep the top few, decide confidence
  pack                    fit neighbours and chunks into the token budget and order them for the model
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..pipeline import Chunk
from .text import jaccard
from .types import RetrievalConfig, StoredChunk


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = 60) -> list[tuple[str, float]]:
    """score(chunk) = sum over lists of 1 / (k + rank). Returns (chunk_id, score) best-first, ties by first appearance."""
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(chunk_id, len(first_seen))
    return sorted(scores.items(), key=lambda item: (-item[1], first_seen[item[0]]))


@dataclass
class Candidate:
    chunk: StoredChunk
    dense: float | None = None  # cosine similarity from the vector search
    sparse: float | None = None  # raw keyword score from the keyword search
    rrf: float = 0.0
    rerank: float | None = None
    relevance: float = 0.0  # absolute 0..1 signal used for the threshold and confidence


@dataclass
class Group:
    """A selected chunk plus the neighbours pulled in next to it."""

    primary: Candidate
    extras: list[StoredChunk] = field(default_factory=list)


def choose(ordered: Sequence[Candidate], cfg: RetrievalConfig, reranked: bool) -> tuple[list[Candidate], str]:
    """-> (kept candidates in rank order, confidence).

    Confidence is `high` only when a real reranker scored the top chunk well above the threshold. If the rerank
    was skipped the evidence is weaker, so confidence is capped at `low` (and the answer will not be cached).
    """
    above = [c for c in ordered if c.relevance >= cfg.min_relevance]
    if not above:
        return [], "none"
    kept: list[Candidate] = []
    for candidate in above:
        if any(jaccard(candidate.chunk.text, other.chunk.text) >= cfg.dedupe_similarity for other in kept):
            continue
        kept.append(candidate)
        if len(kept) == cfg.final_k:
            break
    top = max(c.relevance for c in kept)
    return kept, "high" if reranked and top >= cfg.high_relevance else "low"


def looks_cut(text: str) -> bool:
    """Heuristic for 'this chunk was cut mid-explanation': no sentence-ending punctuation, or starts mid-sentence."""
    stripped = text.strip()
    if not stripped:
        return False
    first_alpha = next((ch for ch in stripped if ch.isalpha()), "")
    return not stripped.endswith((".", "!", "?", ":", ";", ")", '"', "”")) or first_alpha.islower()


def pack(groups: Sequence[Group], cap_tokens: int) -> list[Chunk]:
    """Fit groups into the token budget (best first; the best chunk is always kept), then order for the model.

    Neighbours only get leftover budget after every primary chunk is placed. Each group is emitted in document
    order (so a chunk and its neighbours read as continuous text); groups are ordered best-first with the
    second-best LAST when there are three or more, because models attend most to the start and end of context.
    """
    used, chosen = 0, []
    for group in groups:
        tokens = group.primary.chunk.token_count
        if chosen and used + tokens > cap_tokens:
            continue
        chosen.append(Group(group.primary, []))
        used += tokens
    seen = {g.primary.chunk.id for g in chosen}
    originals = {g.primary.chunk.id: g for g in groups}
    for kept in chosen:
        for extra in originals[kept.primary.chunk.id].extras:
            if extra.id not in seen and used + extra.token_count <= cap_tokens:
                kept.extras.append(extra)
                seen.add(extra.id)
                used += extra.token_count

    if len(chosen) >= 3:
        chosen = [chosen[0], *chosen[2:], chosen[1]]
    out: list[Chunk] = []
    for group in chosen:
        members = [(group.primary.chunk, group.primary.relevance)]
        members += [(extra, group.primary.relevance * 0.5) for extra in group.extras]
        for stored, score in sorted(members, key=lambda m: m[0].ordinal):
            out.append(Chunk(stored.id, stored.text, stored.document_id, stored.version_id,
                             page=stored.page, heading=stored.heading, score=round(score, 4)))
    return out
