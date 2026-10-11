"""The Model Gateway (LLD 09 Part B): one place that talks to providers.

Responsibilities, in the order a call meets them:
  1. size check (reject, never trim: trimming would break the [S#] <-> sources mapping)
  2. lane slot (live vs bulk capacity) -> `Busy` when full
  3. per target (primary, then fallback): circuit breaker, provider rate limit, up to 2 attempts
     (one retry on transient errors with jitter, or a short Retry-After)
  4. first-token deadline; failover is only possible BEFORE the first token
  5. after the first token any failure is `StreamInterrupted` (streams cannot be spliced across providers)
  6. one `CallRecord` per call (never prompt/answer text), written to the ledger even when the call is cancelled
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import AsyncIterator, Awaitable, Callable, Mapping, Sequence

from .ledger import CallRecord, UsageLedger
from .resilience import CircuitBreaker, Lanes, TokenBucket
from .types import (AllProvidersFailed, Busy, ChatRequest, Delta, Done, GatewayConfig, ModelRoute, ProviderError,
                    ProviderRequest, RequestTooLarge, RoutingTable, StreamInterrupted, Target, Usage,
                    estimate_tokens)


class _Call:
    """Mutable bookkeeping for one gateway call; turned into a frozen `CallRecord` at the end."""

    def __init__(self, route: ModelRoute, tenant_id: str, user_id: str, trace_id: str, lane: str, tokens_in: int):
        self.route, self.tenant_id, self.user_id, self.trace_id, self.lane = route, tenant_id, user_id, trace_id, lane
        self.tokens_in_estimate = tokens_in
        self.target: Target | None = None
        self.attempts = self.retries = 0
        self.failover = False
        self.outcome = "cancelled"  # replaced on every normal exit; stays if the task is cancelled
        self.error_kind: str | None = None
        self.ttft_ms: float | None = None
        self.usage: Usage | None = None
        self.tokens_out_estimate = 0


class ModelGateway:
    def __init__(self, routing: RoutingTable, providers: Mapping[str, object], config: GatewayConfig | None = None, *,
                 ledger: UsageLedger | None = None, on_call: Callable[[CallRecord], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 rng: Callable[[], float] = random.random):
        self.routing, self.providers, self.config = routing, dict(providers), config or GatewayConfig()
        self.ledger, self.on_call, self._clock, self._sleep, self._rng = ledger, on_call, clock, sleep, rng
        cfg = self.config
        self.lanes = Lanes(cfg.live_slots, cfg.bulk_slots)
        self.breakers = {name: CircuitBreaker(cfg.breaker_failures, cfg.breaker_window_s, cfg.breaker_cooloff_s, clock)
                         for name in self.providers}
        self.buckets = {name: TokenBucket(limits.requests_per_minute / 60.0, limits.burst, clock, sleep)
                        for name, limits in cfg.provider_limits.items() if name in self.providers}

    # ------------------------------------------------------------------------------------------ helpers
    def _targets(self, route: ModelRoute) -> list[Target]:
        return [t for t in (route.primary, route.fallback) if t is not None]

    async def _pause_before_retry(self, err: ProviderError) -> bool:
        """Wait briefly before the single retry. False means the wait is too long: fail over instead."""
        cfg = self.config
        base = err.retry_after_s or 0.0
        if base > cfg.max_retry_wait_s:
            return False
        await self._sleep(base + self._rng() * cfg.retry_jitter_s)
        return True

    async def _throttle(self, provider: str) -> None:
        bucket = self.buckets.get(provider)
        if bucket is not None:
            await bucket.acquire(self.config.queue_wait_s)

    def _finish(self, call: _Call, started: float, req_meta_model: str) -> None:
        cfg = self.config
        if call.usage is not None:
            tin, tout, estimated = call.usage.input_tokens, call.usage.output_tokens, False
        else:
            tin, tout, estimated = call.tokens_in_estimate, call.tokens_out_estimate, True
        model = call.target.model if call.target else None
        price = cfg.prices.get(model) if model else None
        cost = (tin * price[0] + tout * price[1]) / 1_000_000 if price else None
        if call.target is None:  # nothing was ever sent to a provider: no spend
            tin = tout = 0
        record = CallRecord(
            logical_model=req_meta_model, provider=call.target.provider if call.target else None, model=model,
            lane=call.lane, tenant_id=call.tenant_id, user_id=call.user_id, trace_id=call.trace_id,
            routing_version=self.routing.version, outcome=call.outcome, error_kind=call.error_kind,
            ttft_ms=call.ttft_ms, total_ms=(self._clock() - started) * 1000.0, tokens_in=tin, tokens_out=tout,
            tokens_estimated=estimated, cost_usd=cost, attempts=call.attempts, retries=call.retries,
            failover=call.failover)
        self._last_record = record
        if self.ledger is not None:
            self.ledger.record(call.tenant_id, call.user_id, tin, tout, cost)
        if self.on_call is not None:
            try:
                self.on_call(record)
            except Exception:  # a broken log sink must never break a student's answer
                pass

    # ------------------------------------------------------------------------------------------ chat
    async def stream_chat(self, req: ChatRequest, *,
                          on_record: Callable[[CallRecord], None] | None = None) -> AsyncIterator[Delta | Done]:
        route = self.routing.get(req.logical_model, "chat")
        tokens_in = estimate_tokens(req.system) + sum(estimate_tokens(m.content) for m in req.messages)
        if tokens_in > route.max_input_tokens:
            raise RequestTooLarge(f"input is about {tokens_in} tokens, the limit for {route.logical} is "
                                  f"{route.max_input_tokens}")
        max_out = min(req.max_output_tokens or route.max_output_tokens, route.max_output_tokens)
        call = _Call(route, req.tenant_id, req.user_id, req.trace_id, req.lane, tokens_in)
        started = self._clock()
        deadline = started + route.total_timeout_s
        async with self.lanes.slot(req.lane, self.config.queue_wait_s):
            iterator = None
            try:
                iterator, first = await self._open_stream(route, req, call, max_out, started, deadline)
                event = first
                while True:
                    if isinstance(event, Delta):
                        call.tokens_out_estimate += estimate_tokens(event.text)
                    elif isinstance(event, Done):
                        call.usage = event.usage
                    yield event
                    try:
                        event = await asyncio.wait_for(iterator.__anext__(), max(deadline - self._clock(), 0.001))
                    except StopAsyncIteration:
                        break
                    except (ProviderError, TimeoutError) as exc:
                        kind = exc.kind if isinstance(exc, ProviderError) else "timeout"
                        self._breaker(call).record_failure() if kind in ProviderError.COUNTS_FOR_BREAKER else None
                        call.outcome, call.error_kind = "interrupted", kind
                        raise StreamInterrupted(f"provider stream failed after the first token ({kind})") from None
                call.outcome = "ok"
            except (AllProvidersFailed, Busy, ProviderError) as exc:
                call.outcome = "error"
                call.error_kind = getattr(exc, "kind", type(exc).__name__.lower())
                raise
            except StreamInterrupted:
                raise
            finally:
                if iterator is not None and hasattr(iterator, "aclose"):
                    try:
                        await iterator.aclose()
                    except BaseException:  # noqa: BLE001 - closing must not mask the real outcome
                        pass
                if call.outcome == "cancelled" and call.target is not None:
                    self._breaker(call).cancel_probe()
                self._finish(call, started, req.logical_model)
                if on_record is not None:
                    on_record(self._last_record)

    def _breaker(self, call: _Call) -> CircuitBreaker:
        return self.breakers[call.target.provider]  # type: ignore[union-attr]

    async def _open_stream(self, route: ModelRoute, req: ChatRequest, call: _Call, max_out: int, started: float,
                           deadline: float):
        last_kind, only_busy = "no_target", True
        for index, target in enumerate(self._targets(route)):
            provider, breaker = self.providers.get(target.provider), self.breakers.get(target.provider)
            if provider is None or breaker is None:
                last_kind, only_busy = "unconfigured", False
                continue
            if not breaker.allow():
                last_kind, only_busy = "circuit_open", False
                continue
            preq = ProviderRequest(target.model, req.system, req.messages, max_out)
            for attempt in range(2):
                try:
                    await self._throttle(target.provider)
                except Busy:
                    breaker.cancel_probe()
                    last_kind = "rate_limit"
                    break
                only_busy = False
                call.target, call.attempts, call.failover = target, call.attempts + 1, index > 0
                iterator = provider.stream(preq).__aiter__()  # type: ignore[attr-defined]
                try:
                    wait = min(route.ttft_timeout_s, max(deadline - self._clock(), 0.001))
                    try:
                        first = await asyncio.wait_for(iterator.__anext__(), wait)
                    except TimeoutError:
                        raise ProviderError("timeout", "no first token in time") from None
                    except StopAsyncIteration:
                        raise ProviderError("server", "empty stream") from None
                except ProviderError as err:
                    if hasattr(iterator, "aclose"):
                        try:
                            await iterator.aclose()
                        except BaseException:  # noqa: BLE001
                            pass
                    last_kind = err.kind
                    if err.kind == "bad_request":
                        breaker.cancel_probe()
                        raise  # our request is wrong; another provider will not fix it
                    if err.counts_for_breaker:
                        breaker.record_failure()
                    else:
                        breaker.cancel_probe()
                    if err.retryable and attempt == 0 and await self._pause_before_retry(err):
                        call.retries += 1
                        continue
                    break
                except BaseException:
                    breaker.cancel_probe()
                    raise
                call.ttft_ms = (self._clock() - started) * 1000.0
                breaker.record_success()
                return iterator, first
        if only_busy and last_kind == "rate_limit":
            raise Busy("every provider is at its rate limit")
        raise AllProvidersFailed(last_kind)

    # ------------------------------------------------------------------------------------------ embed / rerank
    async def _unary(self, kind: str, logical: str, texts: Sequence[str], tenant_id: str, user_id: str, trace_id: str,
                     lane: str, invoke: Callable[[object, Target], Awaitable[object]]):
        route = self.routing.get(logical, kind)
        tokens_in = sum(estimate_tokens(t) for t in texts)
        if tokens_in > route.max_input_tokens:
            raise RequestTooLarge(f"input is about {tokens_in} tokens, the limit for {logical} is "
                                  f"{route.max_input_tokens}")
        call = _Call(route, tenant_id, user_id, trace_id, lane, tokens_in)
        started = self._clock()
        async with self.lanes.slot(lane, self.config.queue_wait_s):
            try:
                last_kind, only_busy = "no_target", True
                for index, target in enumerate(self._targets(route)):
                    provider, breaker = self.providers.get(target.provider), self.breakers.get(target.provider)
                    if provider is None or breaker is None:
                        last_kind, only_busy = "unconfigured", False
                        continue
                    if not breaker.allow():
                        last_kind, only_busy = "circuit_open", False
                        continue
                    for attempt in range(2):
                        try:
                            await self._throttle(target.provider)
                        except Busy:
                            breaker.cancel_probe()
                            last_kind = "rate_limit"
                            break
                        only_busy = False
                        call.target, call.attempts, call.failover = target, call.attempts + 1, index > 0
                        try:
                            result = await asyncio.wait_for(invoke(provider, target), route.total_timeout_s)
                        except TimeoutError:
                            err = ProviderError("timeout", "call timed out")
                        except ProviderError as exc:
                            err = exc
                        except BaseException:
                            breaker.cancel_probe()
                            raise
                        else:
                            breaker.record_success()
                            call.outcome = "ok"
                            call.ttft_ms = (self._clock() - started) * 1000.0
                            return result
                        last_kind = err.kind
                        if err.kind == "bad_request":
                            breaker.cancel_probe()
                            raise err
                        breaker.record_failure() if err.counts_for_breaker else breaker.cancel_probe()
                        if err.retryable and attempt == 0 and await self._pause_before_retry(err):
                            call.retries += 1
                            continue
                        break
                if only_busy and last_kind == "rate_limit":
                    raise Busy("every provider is at its rate limit")
                raise AllProvidersFailed(last_kind)
            except (AllProvidersFailed, Busy, ProviderError) as exc:
                call.outcome, call.error_kind = "error", getattr(exc, "kind", type(exc).__name__.lower())
                raise
            finally:
                self._finish(call, started, logical)

    async def embed(self, logical: str, texts: Sequence[str], *, tenant_id: str = "", user_id: str = "",
                    trace_id: str = "", lane: str = "live") -> list[list[float]]:
        async def invoke(provider, target):
            return await provider.embed(target.model, list(texts))
        return await self._unary("embed", logical, texts, tenant_id, user_id, trace_id, lane, invoke)  # type: ignore[return-value]

    async def rerank(self, logical: str, query: str, texts: Sequence[str], *, tenant_id: str = "", user_id: str = "",
                     trace_id: str = "", lane: str = "live") -> list[float]:
        async def invoke(provider, target):
            return await provider.rerank(target.model, query, list(texts))
        return await self._unary("rerank", logical, [query, *texts], tenant_id, user_id, trace_id, lane, invoke)  # type: ignore[return-value]
