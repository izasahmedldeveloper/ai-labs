"""Chat orchestrator pipeline (LLD 03).

Input guardrails || cache -> router -> (retriever) -> model -> chunked output guardrails -> SSE events.
Stages are injected, so tests use fakes and production wires real services.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from typing import AsyncIterator, Callable

from .context import BudgetState, RequestContext
from .runner import PipelineConfig, Refused, StageFailed, record, run_stage
from .streams import ConcurrencyLimiter, StreamBuffer
from .types import (CacheHit, ChunkAction, Chunk, Escalation, Event, Generator, GuardAction, InputGuardrails,
                    OutputGuardrails, Retriever, Route, RouteDecision, Router, SemanticCache)

import time

DEFAULT_ROUTE = RouteDecision(Route.CHEAP_LLM, True, "default: router unavailable")
NONE_FOUND_MESSAGE = ("I couldn't find this in your course notes. You can rephrase the question "
                      "or ask a tutor.")
BLOCKED_MESSAGE = "I can only help with questions about your course notes."
OUTPUT_BLOCKED_MESSAGE = "I can't share that answer. You can rephrase the question or ask a tutor."
BUDGET_MESSAGE = "Your university's usage limit has been reached. Please contact your administrator."
BUSY_MESSAGE = "You already have questions in progress. Please wait a moment."
ESCALATED_MESSAGE = "I've passed this to your tutor. They'll reply here as soon as they can."
UNRELIABLE_NOTICE = "Part of this answer could not be fully verified against your notes."

_SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")


def find_cut(buffer: str, first: bool, cfg: PipelineConfig) -> int | None:
    """Where to release the next chunk: after a sentence end, or at a word boundary once long enough.
    The first chunk is kept short to protect time-to-first-token."""
    limit = cfg.first_chunk_chars if first else cfg.chunk_chars
    match = _SENTENCE_END.search(buffer)
    if match:
        return match.end()
    if len(buffer) >= limit:
        space = buffer.rfind(" ", 0, limit)
        return (space + 1) if space > 0 else limit
    return None


def words(text: str) -> list[str]:
    return re.findall(r"\S+\s*", text)


class Orchestrator:
    def __init__(
        self,
        *,
        guardrails: InputGuardrails,
        cache: SemanticCache,
        router: Router,
        retriever: Retriever,
        generator: Generator,
        output_guardrails: OutputGuardrails,
        escalation: Escalation,
        config: PipelineConfig | None = None,
        on_complete: Callable[[RequestContext], None] | None = None,
    ):
        self.guardrails = guardrails
        self.cache = cache
        self.router = router
        self.retriever = retriever
        self.generator = generator
        self.output = output_guardrails
        self.escalation = escalation
        self.cfg = config or PipelineConfig()
        self.on_complete = on_complete
        self._limiter = ConcurrencyLimiter(self.cfg.max_streams_per_user, self.cfg.max_streams_per_tenant)
        self._streams: dict[tuple[str, str, str], StreamBuffer] = {}

    # ------------------------------------------------------------------ public entry point
    async def handle(self, ctx: RequestContext) -> AsyncIterator[Event]:
        """Yields SSE events. Same request_id twice -> replay/reattach, never a second pipeline run."""
        who = ctx.identity
        key = (who.tenant_id, who.user_id, ctx.request_id)
        buf = self._streams.get(key)
        if buf is not None and buf.cancelled:  # earlier attempt was cancelled: a retry starts fresh
            del self._streams[key]
            buf = None
        if buf is None:
            if not self._limiter.try_acquire(who):
                ctx.outcome = "busy"
                yield Event("error", {"code": "busy", "message": BUSY_MESSAGE, "retryable": True})
                return
            buf = StreamBuffer()
            self._streams[key] = buf
            task = asyncio.create_task(self._run(ctx, buf, key))
            buf.on_abandon = task.cancel
        async for event in buf.subscribe(self.cfg.disconnect_grace_s):
            yield event

    async def _run(self, ctx: RequestContext, buf: StreamBuffer, key) -> None:
        try:
            await self._produce(ctx, buf.append)
        except asyncio.CancelledError:
            buf.cancelled = True
            ctx.outcome = "cancelled"
        finally:
            buf.close()
            abandon = getattr(self.cache, "abandon", None)  # release a cache single-flight marker (LLD 06 s7)
            if abandon is not None:
                abandon(ctx)
            self._limiter.release(ctx.identity)
            if self.on_complete is not None:
                try:  # persistence/metrics hook: sync or async, and a failure here must never touch the answer
                    result = self.on_complete(ctx)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass
            asyncio.get_running_loop().call_later(self.cfg.idempotency_ttl_s, self._streams.pop, key, None)

    # ------------------------------------------------------------------ the pipeline
    async def _produce(self, ctx: RequestContext, emit: Callable[[Event], None]) -> None:
        emit(Event("start", {"request_id": ctx.request_id, "trace_id": ctx.trace_id}))
        try:
            await self._pipeline(ctx, emit)
        except StageFailed as failed:
            ctx.outcome = "error"
            emit(Event("error", {"code": f"{failed.stage}_{failed.outcome}", "message": failed.user_message,
                                 "retryable": True}))
        except Refused as refused:
            ctx.outcome = "blocked" if refused.code == "blocked" else "error"
            emit(Event("error", {"code": refused.code, "message": refused.user_message, "retryable": False}))

    async def _pipeline(self, ctx: RequestContext, emit: Callable[[Event], None]) -> None:
        cfg = self.cfg
        if ctx.budget_state == BudgetState.HARD_LIMIT:  # the gateway normally stops this; defence in depth
            raise Refused("budget_exhausted", BUDGET_MESSAGE)

        # PII is redacted by the fast rule layer BEFORE anything is embedded, cached or logged (LLD 08).
        ctx.original_question = ctx.question
        ctx.question = self.guardrails.sanitize(ctx.question)

        # Input guardrails and cache lookup run in parallel; a guardrail block cancels the cache result.
        guard_task = asyncio.create_task(run_stage(
            ctx, "input_guardrails", True, cfg.guardrails_timeout_s, lambda: self.guardrails.check(ctx)))
        cache_task = asyncio.create_task(run_stage(
            ctx, "cache", False, cfg.cache_timeout_s, lambda: self.cache.lookup(ctx)))
        try:
            guard = await guard_task
        except BaseException:
            await self._cancel(cache_task)
            raise
        ctx.guardrail_action = guard.action.value
        ctx.policy_version = getattr(self.guardrails, "policy_version", None)
        if guard.action == GuardAction.BLOCK:
            await self._cancel(cache_task)
            raise Refused("blocked", BLOCKED_MESSAGE)
        if guard.action == GuardAction.REDACT and guard.text is not None:
            await self._cancel(cache_task)
            ctx.question = guard.text
            ctx.question_embedding = None  # a cancelled lookup may have embedded the unredacted text
            cache_task = asyncio.create_task(run_stage(
                ctx, "cache", False, cfg.cache_timeout_s, lambda: self.cache.lookup(ctx)))
        hit: CacheHit | None = await cache_task
        if hit is not None:
            ctx.cache_result = "hit"
            ctx.route = Route.CACHE.value
            ctx.answer = hit.answer
            ctx.cacheable = False  # already cached
            for piece in words(hit.answer):
                emit(Event("token", {"text": piece}))
            ctx.outcome = "ok"
            emit(Event("done", {"sources": list(hit.sources), "route": ctx.route, "cached": True}))
            return
        ctx.cache_result = "miss" if any(t.stage == "cache" and t.outcome == "ok" for t in ctx.timings) else "skipped"

        decision = await run_stage(ctx, "router", False, cfg.router_timeout_s,
                                   lambda: self.router.route(ctx), fallback=DEFAULT_ROUTE)
        if ctx.budget_state == BudgetState.SOFT_LIMIT and decision.route == Route.STRONG_LLM:
            decision = RouteDecision(Route.CHEAP_LLM, decision.needs_retrieval, "budget soft limit")
        ctx.route = decision.route.value
        ctx.route_reason = decision.reason

        if decision.route in (Route.RULE, Route.REDIRECT):
            ctx.answer = decision.canned_answer or ""
            for piece in words(ctx.answer):
                emit(Event("token", {"text": piece}))
            ctx.outcome = "ok"
            emit(Event("done", {"sources": [], "route": ctx.route}))
            return

        if decision.route == Route.HUMAN:
            ticket_id = await run_stage(ctx, "escalation", True, cfg.escalation_timeout_s,
                                        lambda: self.escalation.create(ctx, decision.reason or "router"))
            message = decision.canned_answer or ESCALATED_MESSAGE  # safety rules supply a supportive text
            ctx.answer = message
            for piece in words(message):
                emit(Event("token", {"text": piece}))
            emit(Event("ticket", {"ticket_id": ticket_id}))
            ctx.outcome = "escalated"
            emit(Event("done", {"sources": [], "route": ctx.route, "ticket_id": ticket_id}))
            return

        chunks: tuple[Chunk, ...] = ()
        if decision.needs_retrieval:
            # Required for note-based routes: never let the model answer from nothing (LLD 03 section 4).
            result = await run_stage(ctx, "retriever", True, cfg.retriever_timeout_s,
                                     lambda: self.retriever.retrieve(ctx))
            ctx.retrieval_confidence = result.confidence
            if result.none_found:
                ctx.answer = NONE_FOUND_MESSAGE
                ctx.cacheable = False
                for piece in words(NONE_FOUND_MESSAGE):
                    emit(Event("token", {"text": piece}))
                ctx.outcome = "none_found"
                emit(Event("done", {"sources": [], "route": ctx.route, "none_found": True}))
                return
            chunks = result.chunks
            ctx.chunks = chunks

        completed = await self._stream_answer(ctx, chunks, emit)
        if not completed:
            return

        sources = tuple(c.source() for c in chunks)
        ctx.outcome = "ok"
        emit(Event("done", {"sources": list(sources), "route": ctx.route, "notices": list(ctx.notices),
                            "usage": dict(ctx.usage)}))
        if ctx.cacheable and ctx.retrieval_confidence == "high":
            # Off the hot path: the answer is already delivered. Cache is optional, so failures are swallowed.
            await run_stage(ctx, "cache_store", False, cfg.cache_timeout_s,
                            lambda: self.cache.store(ctx, ctx.answer, sources))

    # ------------------------------------------------------------------ model + chunked output checks
    async def _open_model(self, ctx: RequestContext, chunks: tuple[Chunk, ...]):
        """Start the model stream; retry once if it fails before the first token (no mid-stream failover)."""
        for attempt in (1, 2):
            t0 = time.monotonic()
            stream = self.generator.stream(ctx, chunks).__aiter__()
            try:
                first = await asyncio.wait_for(stream.__anext__(), self.cfg.model_ttft_timeout_s)
            except StopAsyncIteration:
                record(ctx, "model_ttft", "ok", t0)
                return stream, None
            except Exception:
                record(ctx, "model_ttft", "failed", t0)
                await self._aclose(stream)
                continue
            record(ctx, "model_ttft", "ok", t0)
            ctx.usage["model_attempts"] = attempt
            return stream, first
        raise StageFailed("model", "error")

    async def _stream_answer(self, ctx: RequestContext, chunks: tuple[Chunk, ...],
                             emit: Callable[[Event], None]) -> bool:
        stream, first_token = await self._open_model(ctx, chunks)
        buffer, first_chunk, parts = "", True, []
        try:
            async def tokens():
                if first_token is not None:
                    yield first_token
                async for token in stream:
                    yield token

            gen = tokens()
            async for token in gen:
                buffer += token
                while (cut := find_cut(buffer, first_chunk, self.cfg)) is not None:
                    piece, buffer = buffer[:cut], buffer[cut:]
                    ok = await self._release(ctx, piece, first_chunk, parts, emit)
                    first_chunk = False
                    if not ok:
                        return False
            if buffer:
                if not await self._release(ctx, buffer, first_chunk, parts, emit):
                    return False
        except StageFailed:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # model died mid-stream: no splicing providers, tell the user honestly
            ctx.cacheable = False
            raise StageFailed("model", "error") from exc
        finally:
            await self._aclose(locals().get("gen"))
            await self._aclose(stream)

        ctx.answer = "".join(parts)
        ctx.usage["tokens_out_estimate"] = len(words(ctx.answer))
        grounded = await run_stage(ctx, "output_guardrails_final", True, self.cfg.output_check_timeout_s,
                                   lambda: self.output.final_ok(ctx, ctx.answer, chunks))
        if not grounded:
            ctx.notices.append(UNRELIABLE_NOTICE)
            ctx.cacheable = False
            if ctx.route == Route.CHEAP_LLM.value:  # under-routing signal for LLD 07 s9 (the cascade itself is not built)
                ctx.router_trace["cheap_failed_final_check"] = True
            emit(Event("notice", {"message": UNRELIABLE_NOTICE}))
        return True

    async def _release(self, ctx: RequestContext, piece: str, first: bool, parts: list[str],
                       emit: Callable[[Event], None]) -> bool:
        """Check one chunk, then send it. Nothing reaches the browser before it passes (LLD 03 option C)."""
        decision = await run_stage(ctx, "output_guardrails", True, self.cfg.output_check_timeout_s,
                                   lambda: self.output.check_chunk(ctx, piece, first))
        if decision.action == ChunkAction.BLOCK:
            ctx.cacheable = False
            ctx.outcome = "blocked"
            ctx.answer = "".join(parts)
            emit(Event("error", {"code": "output_blocked", "message": OUTPUT_BLOCKED_MESSAGE,
                                 "retryable": False}))
            return False
        text = decision.text if (decision.action == ChunkAction.REDACT and decision.text is not None) else piece
        parts.append(text)
        emit(Event("token", {"text": text}))
        return True

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _cancel(task: asyncio.Task) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def _aclose(stream) -> None:
        close = getattr(stream, "aclose", None) if stream is not None else None
        if close is not None:
            try:
                await close()
            except Exception:
                pass
