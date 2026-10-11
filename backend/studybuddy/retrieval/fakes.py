"""Deterministic stand-ins for the services the retriever depends on (embedding model, reranker, rewriter).

They let the retriever run and be tested with no network, cost or randomness. They are NOT quality benchmarks:
a hashing embedder only knows the synonyms you give it, and the overlap reranker is a word counter, not a
cross-encoder. Real quality numbers need the real models and a course-specific evaluation set (LLD 05 section 9).
"""
from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Mapping, Sequence

from .text import content_tokens


class HashingEmbedder:
    """Feature-hashing bag-of-words embedder. `synonyms` maps a word to a shared concept so paraphrases match."""

    def __init__(self, dim: int = 256, synonyms: Mapping[str, str] | None = None, delay_s: float = 0.0,
                 fail: bool = False):
        self.dim, self.synonyms, self.delay_s, self.fail, self.calls = dim, dict(synonyms or {}), delay_s, fail, 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("embedding service down")
        return self.embed_sync(texts)

    def embed_sync(self, texts: Sequence[str]) -> list[list[float]]:
        """Synchronous form, for seeding demo data at import time."""
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in content_tokens(text, self.synonyms):
            digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vector[digest % self.dim] += 1.0 if (digest >> 64) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector


class OverlapReranker:
    """Scores each text by the share of the query's content words it contains (0..1)."""

    def __init__(self, synonyms: Mapping[str, str] | None = None, delay_s: float = 0.0, fail: bool = False):
        self.synonyms, self.delay_s, self.fail, self.calls = dict(synonyms or {}), delay_s, fail, 0

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("rerank service down")
        wanted = set(content_tokens(query, self.synonyms))
        if not wanted:
            return [0.0] * len(texts)
        return [len(wanted & set(content_tokens(text, self.synonyms))) / len(wanted) for text in texts]


class JoinRewriter:
    """Pretends to resolve a follow-up by prefixing the previous message. Real rewriting is a small hosted model."""

    def __init__(self, delay_s: float = 0.0, fail: bool = False):
        self.delay_s, self.fail, self.calls = delay_s, fail, 0

    async def rewrite(self, question: str, history: Sequence[str]) -> str:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("rewriter down")
        return f"{history[-1]} {question}"
