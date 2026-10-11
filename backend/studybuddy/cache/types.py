"""Semantic cache contracts (LLD 06): entries, configuration and the store / version-lookup protocols.

`CacheStore` is the Redis boundary: a Redis (RediSearch) implementation only has to provide these six methods.
All logic that matters for correctness (thresholds, freshness, exclusions) lives in `SemanticCache`, not in the store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

Namespace = tuple[str, str, str]  # (tenant_id, course_id, embedding_model_id): lookups cannot cross it (LLD 06 s2)


@dataclass
class CacheEntry:
    id: str  # hash of namespace + normalized question: concurrent duplicate writes are idempotent (s7)
    namespace: Namespace
    question: str  # normalized and already PII-redacted
    embedding: tuple[float, ...]
    answer: str
    sources: tuple[dict, ...]  # as sent in `done.sources`
    tags: tuple[tuple[str, str], ...]  # (document_id, version_id) of every chunk used
    created_at: float
    expires_at: float
    policy_version: str | None
    route: str | None
    hit_count: int = 0
    flags: tuple[str, ...] = ()


class CacheStore(Protocol):
    async def search(self, ns: Namespace, embedding: Sequence[float], k: int) -> list[tuple[CacheEntry, float]]: ...
    async def put(self, entry: CacheEntry) -> None: ...
    async def delete(self, ns: Namespace, entry_id: str) -> bool: ...
    async def touch(self, ns: Namespace, entry_id: str) -> None: ...  # hit_count += 1
    async def delete_document(self, tenant_id: str, course_id: str, document_id: str) -> int: ...  # all namespaces
    async def purge(self, tenant_id: str, course_id: str) -> int: ...


class VersionLookup(Protocol):
    """The document's CURRENT version id (LLD 01), or None if it is deleted or unknown."""

    async def current_version(self, tenant_id: str, course_id: str, document_id: str) -> str | None: ...


@dataclass(frozen=True)
class CacheConfig:
    # D2: 0.92 cosine, "tuned from labeled data". PLACEHOLDER: it is calibrated to nothing real yet, and the demo
    # embedder's similarity scale is not a real model's. Tune on labelled same/different-intent pairs.
    threshold: float = 0.92
    borderline_low: float = 0.88  # D4: below this never; between this and `threshold` a second check must agree
    borderline_min_overlap: float = 0.5  # PLACEHOLDER content-word overlap used by the default second check
    candidates: int = 3
    default_ttl_s: float = 7 * 86400  # D3
    short_ttl_s: float = 86400  # fast-changing courses and deadline-like content
    short_ttl_courses: frozenset[tuple[str, str]] = frozenset()  # (tenant_id, course_id)
    max_answer_chars: int = 8000
    stampede_wait_s: float = 0.0  # D6 says 0.5. OFF by default: the pipeline's cache stage is capped at 50 ms (see README)
    inflight_ttl_s: float = 5.0  # a marker older than this is ignored (its owner died)
