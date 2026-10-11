"""Deterministic fake stages. Used by tests now, and by local demos until real services are wired in.
A fake model makes pipeline tests fast, free and repeatable (LLD 03 section 8)."""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from .context import RequestContext
from .types import (CacheHit, Chunk, ChunkAction, ChunkDecision, GuardAction, GuardrailDecision, RetrievalResult,
                    Route, RouteDecision)

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
ATTACKS = ("ignore your instructions", "ignore your rules", "reveal your system prompt")


class FakeGuardrails:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay, self.fail, self.calls = delay, fail, 0

    def sanitize(self, text: str) -> str:
        return EMAIL.sub("[email]", text)

    async def check(self, ctx: RequestContext) -> GuardrailDecision:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("guardrail service down")
        if any(a in ctx.question.lower() for a in ATTACKS):
            return GuardrailDecision(GuardAction.BLOCK, "injection")
        return GuardrailDecision(GuardAction.ALLOW)


class FakeCache:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay, self.fail = delay, fail
        self.entries: dict[tuple[str, str, str], CacheHit] = {}  # keyed by tenant + course + question
        self.lookups: list[str] = []
        self.stored: list[str] = []

    def _key(self, ctx: RequestContext):
        return (ctx.identity.tenant_id, ctx.course_id, ctx.question.strip().lower())

    async def lookup(self, ctx: RequestContext):
        self.lookups.append(ctx.question)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("redis down")
        return self.entries.get(self._key(ctx))

    async def store(self, ctx: RequestContext, answer: str, sources) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.stored.append(answer)
        self.entries[self._key(ctx)] = CacheHit(answer, tuple(sources))


class FakeRouter:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay, self.fail, self.calls = delay, fail, 0

    async def route(self, ctx: RequestContext) -> RouteDecision:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("router down")
        q = ctx.question.lower().strip()
        if q in ("hi", "hello", "thanks"):
            return RouteDecision(Route.RULE, False, "greeting", canned_answer="Hello! Ask me about your course notes.")
        if "grade" in q and "dispute" in q:
            return RouteDecision(Route.HUMAN, False, "sensitive")
        if len(q.split()) > 15 or "derive" in q:
            return RouteDecision(Route.STRONG_LLM, True, "complex")
        return RouteDecision(Route.CHEAP_LLM, True, "simple")


DEFAULT_CHUNKS = (
    Chunk("c1", "Normalization removes redundancy.", "d1", "v1", page=3, heading="Normal forms", score=0.9),
    Chunk("c2", "BCNF is a stricter form of 3NF.", "d1", "v1", page=4, heading="Normal forms", score=0.8),
)


class FakeRetriever:
    def __init__(self, chunks=DEFAULT_CHUNKS, confidence: str = "high", delay: float = 0.0, fail: bool = False):
        self.chunks, self.confidence, self.delay, self.fail, self.calls = chunks, confidence, delay, fail, 0

    async def retrieve(self, ctx: RequestContext) -> RetrievalResult:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("vector store down")
        if not self.chunks:
            return RetrievalResult((), "none")
        return RetrievalResult(tuple(self.chunks), self.confidence)


class FakeGenerator:
    def __init__(self, text: str = "Normalization removes redundancy. BCNF is stricter than 3NF.",
                 token_delay: float = 0.0, fail_first: int = 0, fail_mid_stream: bool = False, ttft_delay: float = 0.0):
        self.text, self.token_delay, self.fail_first = text, token_delay, fail_first
        self.fail_mid_stream, self.ttft_delay = fail_mid_stream, ttft_delay
        self.calls = 0
        self.started = 0
        self.cancelled = False
        self.finished = False

    async def stream(self, ctx: RequestContext, chunks) -> AsyncIterator[str]:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("provider error")
        self.started += 1
        try:
            await asyncio.sleep(self.ttft_delay)
            pieces = re.findall(r"\S+\s*", self.text)
            for i, piece in enumerate(pieces):
                if self.fail_mid_stream and i == len(pieces) // 2:
                    raise RuntimeError("connection reset")
                await asyncio.sleep(self.token_delay)
                yield piece
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        except GeneratorExit:
            self.cancelled = True
            raise


class FakeOutputGuardrails:
    def __init__(self, block_word: str | None = None, grounded: bool = True, fail: bool = False):
        self.block_word, self.grounded, self.fail = block_word, grounded, fail

    async def check_chunk(self, ctx: RequestContext, text: str, first: bool) -> ChunkDecision:
        if self.fail:
            raise RuntimeError("output guardrail down")
        if self.block_word and self.block_word in text.lower():
            return ChunkDecision(ChunkAction.BLOCK)
        if EMAIL.search(text):
            return ChunkDecision(ChunkAction.REDACT, EMAIL.sub("[email]", text))
        return ChunkDecision(ChunkAction.PASS)

    async def final_ok(self, ctx: RequestContext, answer: str, chunks) -> bool:
        if self.fail:
            raise RuntimeError("output guardrail down")
        return self.grounded


class FakeEscalation:
    def __init__(self, fail: bool = False):
        self.fail, self.tickets = fail, []

    async def create(self, ctx: RequestContext, reason: str) -> str:
        if self.fail:
            raise RuntimeError("db down")
        self.tickets.append((ctx.identity.tenant_id, ctx.question, reason))
        return f"T-{len(self.tickets)}"
