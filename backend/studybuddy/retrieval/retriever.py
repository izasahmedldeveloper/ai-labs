"""Hybrid retriever (LLD 05): the real `Retriever` stage.

Query prep -> vector search || keyword search -> RRF fusion -> rerank -> select, neighbours, pack.

Failure behaviour (LLD 05 sections 2 and 8, LLD 03 section 4): the stage is REQUIRED, but it degrades before it
fails. Over budget it gives up the rerank first, then runs on a single search path; only when no search path
works does it raise, which the orchestrator turns into "couldn't look that up right now". An empty result is
`none_found`, a normal outcome rather than an error. It never calls a generation model.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Sequence

from ..pipeline import RequestContext, RetrievalResult
from ..pipeline.runner import record
from .selection import Candidate, Group, choose, looks_cut, pack, reciprocal_rank_fusion
from .text import lexical_coverage
from .types import (ChunkStore, Embedder, Hit, IsolationViolation, QueryRewriter, Reranker, RetrievalConfig,
                    RetrievalUnavailable, SearchFilter, StoredChunk)

AuditSink = Callable[[dict], None]


def prepare_query(question: str, max_words: int) -> str:
    """Light normalisation only (trim, collapse whitespace) and a length cap; the embedding handles meaning."""
    return " ".join(" ".join(question.split()).split(" ")[:max_words])


class HybridRetriever:
    def __init__(self, store: ChunkStore, embedder: Embedder, *, reranker: Reranker | None = None,
                 rewriter: QueryRewriter | None = None, config: RetrievalConfig | None = None,
                 on_event: AuditSink | None = None):
        self.store, self.embedder = store, embedder
        self.reranker, self.rewriter = reranker, rewriter
        self.cfg = config or RetrievalConfig()
        self.on_event = on_event

    # ------------------------------------------------------------------------------------ entry point
    async def retrieve(self, ctx: RequestContext) -> RetrievalResult:
        cfg = self.cfg
        deadline = time.monotonic() + cfg.total_budget_s
        flt = SearchFilter(ctx.identity.tenant_id, ctx.course_id)  # raises MissingFilterError if scope is missing
        trace: dict = {"degraded": []}
        ctx.retrieval_trace = trace

        original = prepare_query(ctx.question, cfg.max_query_words)
        query = await self._rewrite(ctx, original, trace)
        trace["query"], trace["rewritten_query"] = original, ctx.rewritten_query

        dense, sparse = await asyncio.gather(
            self._search(ctx, "vector_search", lambda: self._dense(ctx, flt, query)),
            self._search(ctx, "keyword_search", lambda: self.store.sparse_search(flt, query, cfg.sparse_k)))
        if dense is None and sparse is None:
            raise RetrievalUnavailable("vector and keyword search both failed")
        for name, hits in (("vector", dense), ("keyword", sparse)):
            if hits is None:
                trace["degraded"].append(f"{name}_search_unavailable")
        dense, sparse = dense or [], sparse or []
        self._verify(flt, [h.chunk for h in dense + sparse])

        candidates = self._fuse(dense, sparse)
        scores = await self._rerank(ctx, query, candidates, deadline, trace)
        if scores is not None:
            for candidate, score in zip(candidates, scores):
                candidate.rerank = candidate.relevance = score
            ordered = sorted(candidates, key=lambda c: (-c.rerank, -c.rrf))
        else:  # fused order (LLD 05 s5 fallback); relevance from absolute signals only
            for candidate in candidates:
                candidate.relevance = max(candidate.dense or 0.0, lexical_coverage(query, candidate.chunk.text))
            ordered = list(candidates)
        trace["reranked"] = scores is not None
        trace["candidates"] = [{"id": c.chunk.id, "dense": c.dense, "sparse": c.sparse, "rrf": round(c.rrf, 5),
                                "relevance": round(c.relevance, 4)} for c in candidates]

        kept, confidence = choose(ordered, cfg, reranked=scores is not None)
        trace["confidence"], trace["final_ranked"] = confidence, [c.chunk.id for c in kept]
        if not kept:
            return RetrievalResult((), "none")

        groups = [Group(c) for c in kept]
        if cfg.neighbour_expansion:
            await self._expand(ctx, flt, groups, deadline)
        chunks = pack(groups, cfg.context_token_cap)
        trace["final"] = [c.id for c in chunks]
        return RetrievalResult(tuple(chunks), confidence)

    # ------------------------------------------------------------------------------------ steps
    async def _rewrite(self, ctx: RequestContext, query: str, trace: dict) -> str:
        """Follow-ups only. Fails open: a failed or slow rewrite just means the original question is searched."""
        if self.rewriter is None or not ctx.history:
            return query
        t0 = time.monotonic()
        try:
            rewritten = await asyncio.wait_for(self.rewriter.rewrite(query, ctx.history), self.cfg.rewrite_timeout_s)
        except Exception:
            record(ctx, "rewrite", "skipped", t0)
            trace["degraded"].append("rewrite_skipped")
            return query
        record(ctx, "rewrite", "ok", t0)
        rewritten = prepare_query(rewritten or "", self.cfg.max_query_words)
        if not rewritten or rewritten == query:
            return query
        ctx.rewritten_query = rewritten  # logged next to the original: rewriting can drift from what was meant
        return rewritten

    async def _dense(self, ctx: RequestContext, flt: SearchFilter, query: str) -> list[Hit]:
        # The embedding is computed once and shared with the cache (LLD 03 s6). A rewritten query needs its own.
        if ctx.question_embedding is None or ctx.rewritten_query is not None:
            ctx.question_embedding = tuple((await self.embedder.embed([query]))[0])
        return await self.store.dense_search(flt, ctx.question_embedding, self.cfg.dense_k)

    async def _search(self, ctx: RequestContext, name: str, call) -> list[Hit] | None:
        t0 = time.monotonic()
        try:
            hits = await asyncio.wait_for(call(), self.cfg.search_timeout_s)
        except Exception:  # CancelledError is not an Exception, so a cancelled request still stops
            record(ctx, name, "failed", t0)
            return None
        record(ctx, name, "ok", t0)
        return hits

    def _fuse(self, dense: Sequence[Hit], sparse: Sequence[Hit]) -> list[Candidate]:
        by_id: dict[str, Candidate] = {}
        for hit in dense:
            by_id.setdefault(hit.chunk.id, Candidate(hit.chunk)).dense = hit.score
        for hit in sparse:
            by_id.setdefault(hit.chunk.id, Candidate(hit.chunk)).sparse = hit.score
        fused = reciprocal_rank_fusion([[h.chunk.id for h in dense], [h.chunk.id for h in sparse]], self.cfg.rrf_k)
        out = []
        for chunk_id, score in fused[: self.cfg.fused_k]:
            by_id[chunk_id].rrf = score
            out.append(by_id[chunk_id])
        return out

    async def _rerank(self, ctx: RequestContext, query: str, candidates: Sequence[Candidate], deadline: float,
                      trace: dict) -> list[float] | None:
        """Optional sub-step: fails open to the fused order and records `rerank: skipped`."""
        t0 = time.monotonic()
        remaining = deadline - t0
        if self.reranker is None or not candidates or remaining < self.cfg.min_rerank_budget_s:
            record(ctx, "rerank", "skipped", t0)
            if self.reranker is not None and candidates:
                trace["degraded"].append("rerank_skipped_over_budget")
            return None
        try:
            scores = await asyncio.wait_for(self.reranker.rerank(query, [c.chunk.text for c in candidates]),
                                            min(self.cfg.rerank_timeout_s, remaining))
            if len(scores) != len(candidates):
                raise ValueError("reranker returned the wrong number of scores")
            scores = [min(1.0, max(0.0, float(s))) for s in scores]
        except Exception:
            record(ctx, "rerank", "skipped", t0)
            trace["degraded"].append("rerank_skipped_error")
            return None
        record(ctx, "rerank", "ok", t0)
        return scores

    async def _expand(self, ctx: RequestContext, flt: SearchFilter, groups: Sequence[Group], deadline: float) -> None:
        """Pull in the previous/next chunk for chunks that look cut mid-explanation. Optional: failures are skipped."""
        for group in groups:
            remaining = deadline - time.monotonic()
            if remaining <= 0.01 or not looks_cut(group.primary.chunk.text):
                continue
            t0 = time.monotonic()
            try:
                found = await asyncio.wait_for(self.store.neighbours(flt, group.primary.chunk),
                                               min(self.cfg.search_timeout_s, remaining))
            except Exception:
                record(ctx, "neighbours", "skipped", t0)
                continue
            self._verify(flt, found)
            group.extras.extend(found)

    def _verify(self, flt: SearchFilter, chunks: Sequence[StoredChunk]) -> None:
        """Tripwire behind the store's own filter (LLD 01 section 3): a stray chunk fails the whole request."""
        for chunk in chunks:
            if chunk.tenant_id != flt.tenant_id or chunk.course_id != flt.course_id:
                if self.on_event is not None:
                    self.on_event({"stage": "retriever", "decision": "isolation_violation", "chunk_id": chunk.id})
                raise IsolationViolation("store returned a chunk outside the requested tenant/course")
