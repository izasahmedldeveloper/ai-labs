"""The semantic cache stage (LLD 06). Correctness over hit rate: when anything is doubtful the answer is a miss.

A stored answer is served only if ALL hold: same tenant+course+embedding-model namespace; similarity >= threshold
(borderline band needs a second same-intent check); numbers, negations and names agree; every source document is still
at the version the answer was built from; not expired; guardrail policy version unchanged. Any error or timeout is a miss.

The stage follows `types.SemanticCache` in the pipeline (`lookup`, `store`) and adds `abandon` (release the stampede
marker when a request ends without storing) plus the invalidation API used by the CDC consumer and admin actions.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from typing import Callable, Sequence

from ..guardrails.text import redact_pii
from ..pipeline import CacheHit, GuardAction, RequestContext
from ..retrieval.types import Embedder
from .text import (content_overlap, is_deadline_like, is_personal, is_time_sensitive, normalize_question, same_signature,
                   signature)
from .types import CacheConfig, CacheEntry, CacheStore, Namespace, VersionLookup


def entry_id(ns: Namespace, normalized: str) -> str:
    return hashlib.sha256("\x1f".join((*ns, normalized)).encode()).hexdigest()[:32]


class SemanticCache:
    def __init__(self, store: CacheStore, embedder: Embedder, versions: VersionLookup, *,
                 embedder_id: str = "embed-v1", config: CacheConfig | None = None,
                 policy_version: Callable[[], str | None] = lambda: None,
                 clock: Callable[[], float] = time.time,
                 intent_check: Callable[[str, str], bool] | None = None):
        self.backend, self.embedder, self.versions = store, embedder, versions
        self.embedder_id, self.cfg, self._policy_version, self._clock = embedder_id, config or CacheConfig(), policy_version, clock
        self._intent_check = intent_check  # optional extra second check for the borderline band (D4)
        self.disabled_tenants: set[str] = set()  # per-tenant kill switch for bad thresholds (s9)
        self.stats: Counter = Counter()
        self._inflight: dict[str, tuple[asyncio.Future, int, float]] = {}  # key -> (future, owner ctx id, started)
        self._owned: dict[int, str] = {}  # ctx id -> key

    # ------------------------------------------------------------------------------------------ helpers
    def namespace(self, ctx: RequestContext) -> Namespace:
        return (ctx.identity.tenant_id, ctx.course_id, self.embedder_id)

    def _bypass(self, ctx: RequestContext, reason: str) -> None:
        ctx.cache_trace.update({"result": "bypass", "reason": reason})
        self.stats[f"bypass:{reason}"] += 1

    def _bypass_reason(self, ctx: RequestContext) -> str | None:
        if ctx.identity.tenant_id in self.disabled_tenants:
            return "tenant_disabled"
        if ctx.history:  # the rewritten standalone question only exists after retrieval; do not guess (s3)
            return "follow_up"
        if is_personal(ctx.question):
            return "personal"
        if is_time_sensitive(ctx.question):
            return "time_sensitive"
        return None

    async def _embedding(self, ctx: RequestContext) -> tuple[float, ...]:
        if ctx.question_embedding is None:  # shared with the retriever, which reuses it (LLD 03 s6)
            ctx.question_embedding = tuple((await self.embedder.embed([ctx.question]))[0])
        return ctx.question_embedding

    async def _fresh(self, ctx: RequestContext, entry: CacheEntry) -> bool:
        """Read-time version check: the safety net behind the CDC events (s5). Cannot verify -> not fresh."""
        tenant, course = ctx.identity.tenant_id, ctx.course_id
        current = await asyncio.gather(*(self.versions.current_version(tenant, course, doc) for doc, _ in entry.tags))
        return all(now is not None and now == version for now, (_, version) in zip(current, entry.tags))

    async def _search(self, ctx: RequestContext, ns: Namespace, normalized: str) -> CacheHit | None:
        cfg = self.cfg
        embedding = await self._embedding(ctx)
        candidates = await self.backend.search(ns, embedding, cfg.candidates)
        sig, now, policy = signature(ctx.question), self._clock(), self._policy_version()
        best = candidates[0][1] if candidates else None
        ctx.cache_trace.update({"best_similarity": round(best, 4) if best is not None else None})
        for entry, similarity in candidates:
            if similarity < cfg.borderline_low:
                break
            if (entry.namespace[0], entry.namespace[1]) != (ns[0], ns[1]):  # tripwire behind the namespace (LLD 01)
                self.stats["isolation_violation"] += 1
                await self.backend.delete(entry.namespace, entry.id)
                continue
            if entry.expires_at <= now:
                self.stats["rejected:expired"] += 1
                await self.backend.delete(ns, entry.id)
                continue
            if entry.policy_version != policy:
                self.stats["rejected:policy_version"] += 1
                continue
            other = signature(entry.question)
            if not same_signature(sig, other):
                self.stats["rejected:signature"] += 1
                continue
            if similarity < cfg.threshold:  # borderline band: second check must agree
                same_intent = content_overlap(sig, other) >= cfg.borderline_min_overlap
                if same_intent and self._intent_check is not None:
                    same_intent = self._intent_check(ctx.question, entry.question)
                if not same_intent:
                    self.stats["rejected:borderline"] += 1
                    continue
            if not await self._fresh(ctx, entry):
                self.stats["rejected:stale"] += 1
                await self.backend.delete(ns, entry.id)  # lazy cleanup if the CDC event was lost
                continue
            await self.backend.touch(ns, entry.id)
            ctx.cache_trace.update({"result": "hit", "similarity": round(similarity, 4), "entry_id": entry.id})
            self.stats["hit"] += 1
            return CacheHit(entry.answer, entry.sources)
        return None

    # ------------------------------------------------------------------------------------------ pipeline API
    async def lookup(self, ctx: RequestContext) -> CacheHit | None:
        reason = self._bypass_reason(ctx)
        if reason is not None:
            self._bypass(ctx, reason)
            return None
        self.stats["lookup"] += 1
        ns, normalized = self.namespace(ctx), normalize_question(ctx.question)
        try:
            hit = await self._search(ctx, ns, normalized)
            if hit is not None:
                return hit
            key = entry_id(ns, normalized)
            marker = self._inflight.get(key)
            now = time.monotonic()
            if marker and (marker[0].done() or now - marker[2] > self.cfg.inflight_ttl_s):
                marker = None
            if marker and marker[1] != id(ctx) and self.cfg.stampede_wait_s > 0:
                # Someone is already computing this exact question: wait briefly for their answer (s7).
                self.stats["stampede_wait"] += 1
                try:
                    await asyncio.wait_for(asyncio.shield(marker[0]), self.cfg.stampede_wait_s)
                except (TimeoutError, asyncio.CancelledError):
                    if asyncio.current_task() and asyncio.current_task().cancelling():  # type: ignore[union-attr]
                        raise
                hit = await self._search(ctx, ns, normalized)
                if hit is not None:
                    ctx.cache_trace["waited"] = True
                    return hit
            elif marker is None and self.cfg.stampede_wait_s > 0:
                # No await between the check and this assignment, so concurrent misses elect exactly one owner.
                self._inflight[key] = (asyncio.get_running_loop().create_future(), id(ctx), now)
                self._owned[id(ctx)] = key
            ctx.cache_trace.update({"result": "miss"})
            self.stats["miss"] += 1
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            self.stats["error"] += 1
            ctx.cache_trace.update({"result": "error"})
            raise  # run_stage turns this into a miss and records the stage outcome

    def _skip_store_reason(self, ctx: RequestContext, answer: str, sources: Sequence[dict]) -> str | None:
        reason = self._bypass_reason(ctx)
        if reason:
            return reason
        if not sources:
            return "no_sources"  # nothing to version-check against, so it could never be proven fresh
        if not answer.strip() or len(answer) > self.cfg.max_answer_chars:
            return "answer_size"
        if redact_pii(answer)[1] or redact_pii(ctx.question)[1]:
            return "pii"
        if ctx.guardrail_action not in (None, GuardAction.ALLOW.value, GuardAction.REDACT.value):
            return "guardrail_action"
        return None

    async def store(self, ctx: RequestContext, answer: str, sources: Sequence[dict]) -> None:
        try:
            reason = self._skip_store_reason(ctx, answer, sources)
            if reason is not None:
                self.stats[f"store_skipped:{reason}"] += 1
                return
            ns, normalized, now = self.namespace(ctx), normalize_question(ctx.question), self._clock()
            short = ((ctx.identity.tenant_id, ctx.course_id) in self.cfg.short_ttl_courses
                     or is_deadline_like(ctx.question) or is_deadline_like(answer))
            ttl = self.cfg.short_ttl_s if short else self.cfg.default_ttl_s
            embedding = await self._embedding(ctx)
            tags = tuple(sorted({(s["document_id"], s["version_id"]) for s in sources}))
            await self.backend.put(CacheEntry(
                id=entry_id(ns, normalized), namespace=ns, question=normalized, embedding=tuple(embedding),
                answer=answer, sources=tuple(dict(s) for s in sources), tags=tags, created_at=now,
                expires_at=now + ttl, policy_version=ctx.policy_version, route=ctx.route))
            self.stats["store"] += 1
        finally:
            self._release(ctx)

    def _release(self, ctx: RequestContext) -> None:
        key = self._owned.pop(id(ctx), None)
        marker = self._inflight.get(key) if key else None
        if marker and marker[1] == id(ctx):
            del self._inflight[key]
            if not marker[0].done():
                marker[0].set_result(None)

    def abandon(self, ctx: RequestContext) -> None:
        """The request ended without storing (blocked, low confidence, error, cancelled): let waiters proceed."""
        self._release(ctx)

    # ------------------------------------------------------------------------------------------ invalidation (s5, s9)
    async def invalidate_document(self, tenant_id: str, course_id: str, document_id: str) -> int:
        removed = await self.backend.delete_document(tenant_id, course_id, document_id)
        self.stats["invalidated"] += removed
        return removed

    async def purge_course(self, tenant_id: str, course_id: str) -> int:
        removed = await self.backend.purge(tenant_id, course_id)
        self.stats["invalidated"] += removed
        return removed

    async def evict(self, tenant_id: str, course_id: str, entry: str) -> bool:
        """Thumbs-down on a cached answer: remove it (it is also flagged for review by the caller)."""
        ok = await self.backend.delete((tenant_id, course_id, self.embedder_id), entry)
        self.stats["evicted"] += int(ok)
        return ok
