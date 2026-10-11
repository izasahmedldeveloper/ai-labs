"""In-memory ChunkStore: dense cosine search plus real BM25, with the same guarantees Qdrant must give.

Used for tests, local demos and the evaluation harness. It is deliberately a faithful stand-in for the contract in
`types.ChunkStore`: tenant+course scoped, current-version only, flagged chunks never returned, best-first results.
It scans every chunk per query, so it is for small corpora only.
"""
from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from typing import Sequence

from .text import STOPWORDS
from .types import Embedder, Hit, MissingFilterError, SearchFilter, StoredChunk

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")


def bm25_tokens(text: str) -> list[str]:
    """Words plus the parts of compound terms, so 'CS-301' and 'Section 3.2' match exactly AND by part."""
    out = []
    for match in _TOKEN.finditer(text.lower()):
        token = match.group()
        out.append(token)
        if any(ch in token for ch in "-_."):
            out.extend(part for part in re.split(r"[-_.]", token) if part)
    return out


def _require(flt: object) -> SearchFilter:
    if not isinstance(flt, SearchFilter):
        raise MissingFilterError("a SearchFilter (tenant_id + course_id) is required for every search")
    return flt


class InMemoryChunkStore:
    def __init__(self, *, delay_s: float = 0.0, fail: bool = False, k1: float = 1.5, b: float = 0.75):
        self.delay_s, self.fail, self.k1, self.b = delay_s, fail, k1, b
        self._chunks: dict[str, StoredChunk] = {}
        self._vectors: dict[str, list[float]] = {}
        # (tenant, course, document) -> the version students may see. Mirrors document.current_version_id.
        self._current: dict[tuple[str, str, str], str | None] = {}

    # ------------------------------------------------------------------------ ingestion side (test helpers)
    def add(self, chunks: Sequence[StoredChunk], vectors: Sequence[Sequence[float]]) -> None:
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._chunks[chunk.id] = chunk
            self._vectors[chunk.id] = list(vector)

    def set_current(self, tenant_id: str, course_id: str, document_id: str, version_id: str | None) -> None:
        self._current[(tenant_id, course_id, document_id)] = version_id

    async def publish(self, chunks: Sequence[StoredChunk], embedder: Embedder) -> None:
        """Index chunks and make their version current in one step (the atomic swap of LLD 04)."""
        vectors = await embedder.embed([c.text for c in chunks])
        self.add(chunks, vectors)
        for chunk in chunks:
            self.set_current(chunk.tenant_id, chunk.course_id, chunk.document_id, chunk.version_id)

    def current_version(self, tenant_id: str, course_id: str, document_id: str) -> str | None:
        return self._current.get((tenant_id, course_id, document_id))

    def delete_version(self, tenant_id: str, course_id: str, document_id: str, version_id: str) -> None:
        for cid in [c.id for c in self._chunks.values() if (c.tenant_id, c.course_id, c.document_id, c.version_id)
                    == (tenant_id, course_id, document_id, version_id)]:
            self._chunks.pop(cid, None)
            self._vectors.pop(cid, None)

    def count(self, tenant_id: str, course_id: str, document_id: str | None = None, version_id: str | None = None) -> int:
        return sum(1 for c in self._chunks.values() if c.tenant_id == tenant_id and c.course_id == course_id
                   and (document_id is None or c.document_id == document_id) and (version_id is None or c.version_id == version_id))

    def remove_document(self, tenant_id: str, course_id: str, document_id: str) -> None:
        self.set_current(tenant_id, course_id, document_id, None)  # chunks stay until cleanup; never served

    # ------------------------------------------------------------------------ eligibility
    def _eligible(self, flt: SearchFilter) -> list[StoredChunk]:
        return [c for c in self._chunks.values()
                if c.tenant_id == flt.tenant_id and c.course_id == flt.course_id and not c.flagged
                and self._current.get((c.tenant_id, c.course_id, c.document_id)) == c.version_id]

    async def _gate(self) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("vector store unavailable")

    # ------------------------------------------------------------------------ ChunkStore protocol
    async def dense_search(self, flt: SearchFilter, embedding: Sequence[float], limit: int) -> list[Hit]:
        _require(flt)
        await self._gate()
        norm = math.sqrt(sum(x * x for x in embedding))
        if norm == 0:
            return []
        hits = []
        for chunk in self._eligible(flt):
            vector = self._vectors.get(chunk.id)
            if vector is None:
                continue
            vnorm = math.sqrt(sum(x * x for x in vector))
            if vnorm == 0:
                continue
            hits.append(Hit(chunk, sum(a * b for a, b in zip(embedding, vector)) / (norm * vnorm)))
        hits.sort(key=lambda h: (-h.score, h.chunk.id))
        return hits[:limit]

    async def sparse_search(self, flt: SearchFilter, query: str, limit: int) -> list[Hit]:
        _require(flt)
        await self._gate()
        corpus = self._eligible(flt)
        terms = {t for t in bm25_tokens(query) if t not in STOPWORDS}
        if not corpus or not terms:
            return []
        docs = {c.id: Counter(bm25_tokens(c.text)) for c in corpus}
        lengths = {cid: sum(tf.values()) for cid, tf in docs.items()}
        avg_len = sum(lengths.values()) / len(lengths)
        n = len(corpus)
        df = {t: sum(1 for tf in docs.values() if t in tf) for t in terms}
        hits = []
        for chunk in corpus:
            tf, length, score = docs[chunk.id], lengths[chunk.id], 0.0
            for term in terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / avg_len))
            if score > 0:
                hits.append(Hit(chunk, score))
        hits.sort(key=lambda h: (-h.score, h.chunk.id))
        return hits[:limit]

    async def neighbours(self, flt: SearchFilter, chunk: StoredChunk) -> list[StoredChunk]:
        _require(flt)
        await self._gate()
        wanted = {chunk.ordinal - 1, chunk.ordinal + 1}
        found = [c for c in self._eligible(flt)
                 if c.document_id == chunk.document_id and c.version_id == chunk.version_id and c.ordinal in wanted]
        return sorted(found, key=lambda c: c.ordinal)
