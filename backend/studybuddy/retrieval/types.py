"""Retriever contracts (LLD 05 section 2): filter, stored chunk, backend protocols, configuration.

The hard rule from LLD 01 section 3 lives here: a `SearchFilter` cannot be built without a tenant and a course,
and every store method takes one. A search that forgot its scope is therefore a programming error that fails
loudly, never a query that quietly spans every tenant.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, Sequence


class MissingFilterError(ValueError):
    """A search was attempted without a tenant and course scope."""


class IsolationViolation(RuntimeError):
    """A backend returned a chunk outside the requested tenant/course. Fatal for the request: fail closed."""


class RetrievalUnavailable(RuntimeError):
    """Nothing usable could be searched (every search path failed or timed out)."""


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (about 1.3 tokens per word); good enough for a context budget."""
    return max(1, math.ceil(len(text.split()) * 1.3))


@dataclass(frozen=True)
class SearchFilter:
    tenant_id: str
    course_id: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "course_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise MissingFilterError(f"{name} is required for every search")


@dataclass(frozen=True)
class StoredChunk:
    """A chunk as held by the vector store (LLD 04 section 3 payload; text kept so vectors can be rebuilt)."""

    id: str
    text: str
    tenant_id: str
    course_id: str
    document_id: str
    version_id: str
    ordinal: int = 0  # position within the document version, used for neighbour expansion
    page: int | None = None
    heading: str | None = None
    flagged: bool = False  # injection suspected at ingestion (LLD 04): never retrieved

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class Hit:
    chunk: StoredChunk
    score: float  # dense: cosine similarity; sparse: raw keyword score (only comparable within one list)


class ChunkStore(Protocol):
    """What the retriever needs from Qdrant (or any vector store). Implementations MUST apply the tenant/course
    filter and serve only current-version, unflagged chunks, and MUST return results best-first."""

    async def dense_search(self, flt: SearchFilter, embedding: Sequence[float], limit: int) -> list[Hit]: ...
    async def sparse_search(self, flt: SearchFilter, query: str, limit: int) -> list[Hit]: ...
    async def neighbours(self, flt: SearchFilter, chunk: StoredChunk) -> list[StoredChunk]: ...


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        """One relevance score in [0, 1] per text, same order. Cross-encoder in production (LLD 05 D4)."""
        ...


class QueryRewriter(Protocol):
    async def rewrite(self, question: str, history: Sequence[str]) -> str:
        """Standalone version of a follow-up question, using recent history."""
        ...


@dataclass(frozen=True)
class RetrievalConfig:
    # LLD 05 D1: candidates per search / after fusion / after rerank
    dense_k: int = 30
    sparse_k: int = 30
    fused_k: int = 20
    final_k: int = 5
    rrf_k: int = 60
    context_token_cap: int = 2000  # D2
    # D5: thresholds are PLACEHOLDERS. They must be tuned on the evaluation set (LLD 05 section 9), per reranker.
    min_relevance: float = 0.30
    high_relevance: float = 0.60
    dedupe_similarity: float = 0.90
    neighbour_expansion: bool = True  # D7
    max_query_words: int = 200
    # Time budget (LLD 05 section 8). The stage as a whole must finish inside the orchestrator's retriever timeout.
    total_budget_s: float = 0.28
    search_timeout_s: float = 0.10
    rerank_timeout_s: float = 0.15
    rewrite_timeout_s: float = 0.10
    min_rerank_budget_s: float = 0.03  # less time than this left -> skip the rerank (first thing to give up)
