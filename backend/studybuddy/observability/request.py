"""Per-request telemetry (LLD 11 s3-s6): metrics, one structured log line, and a sampled trace for every chat request.

Install with `Orchestrator(on_complete=chain(observer.on_complete, persist))`. Nothing here ever records the question or the
answer; only ids, outcomes, timings, counts and cost.
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .metrics import Registry
from .scrub import JsonLogger

GOOD_OUTCOMES = frozenset({"ok", "blocked", "none_found", "escalated", "cancelled"})  # "busy" is back-pressure, not counted


@dataclass
class Span:
    name: str
    start_ms: float
    duration_ms: float
    status: str = "ok"
    attrs: dict = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    request_id: str
    tenant: str
    outcome: str
    total_ms: float
    sampled_because: str
    spans: list[Span]


class SpanExporter(Protocol):
    def export(self, trace: Trace) -> None: ...


class MemoryExporter:
    def __init__(self) -> None:
        self.traces: list[Trace] = []

    def export(self, trace: Trace) -> None:
        self.traces.append(trace)


class TraceSampler:
    """Keep every error, blocked, escalated or slow request; a fixed fraction of the rest (LLD 11 s4).

    The fraction is decided from the trace id, so it is stable and needs no randomness."""

    def __init__(self, rate: float = 0.10, slow_ms: float = 6000):
        if not 0 <= rate <= 1:
            raise ValueError("rate must be between 0 and 1")
        self.rate, self.slow_ms = rate, slow_ms

    def decide(self, outcome: str, total_ms: float, trace_id: str) -> str | None:
        if outcome == "error":
            return "error"
        if outcome == "blocked":
            return "blocked"
        if outcome == "escalated":
            return "escalated"
        if total_ms >= self.slow_ms:
            return "slow"
        bucket = int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return "sampled" if bucket < self.rate else None


class Telemetry:
    """The metric set (names follow LLD 11 s3). Every request metric carries a `tenant` label for the tenant view."""

    def __init__(self, registry: Registry):
        r = registry
        self.requests = r.counter("sb_requests_total", "Chat requests by outcome", ("tenant", "outcome", "route"))
        self.cache = r.counter("sb_cache_requests_total", "Semantic cache lookups", ("tenant", "result"))
        self.latency = r.histogram("sb_request_ms", "End-to-end request time", ("tenant",))
        self.ttft = r.histogram("sb_ttft_ms", "Time to first model token", ("tenant",))
        self.stage = r.histogram("sb_stage_ms", "Per-stage time", ("stage",))
        self.stage_fail = r.counter("sb_stage_failures_total", "Stages that did not finish ok", ("stage", "outcome"))
        self.tok_in = r.counter("sb_tokens_in_total", "Prompt tokens", ("tenant",))
        self.tok_out = r.counter("sb_tokens_out_total", "Completion tokens", ("tenant",))
        self.cost = r.counter("sb_cost_usd_total", "Model spend", ("tenant",))
        self.failover = r.counter("sb_provider_failover_total", "Calls served by the fallback provider", ("tenant",))
        self.guard = r.counter("sb_guardrail_blocks_total", "Requests blocked by guardrails", ("tenant",))
        self.none_found = r.counter("sb_none_found_total", "Questions with no relevant material", ("tenant",))
        self.escalations = r.counter("sb_escalations_total", "Requests handed to a human", ("tenant",))
        self.events = r.counter("sb_events_total", "Component events", ("source", "decision"))
        self.violations = r.counter("sb_isolation_violations_total", "Cross-tenant data seen where it must not be", ())
        self.pii_leaks = r.counter("sb_pii_leaks_total", "PII found in logs or traces", ())
        self.dead_letters = r.counter("sb_ingest_dead_letters_total", "Documents sent to the dead-letter topic", ())
        self.urgent_unclaimed = r.gauge("sb_urgent_tickets_unclaimed", "Urgent tickets waiting for a tutor", ())
        self.canary = r.counter("sb_canary_runs_total", "Canary probes", ("result",))


class RequestObserver:
    def __init__(self, registry: Registry, *, logger: JsonLogger | None = None, exporter: SpanExporter | None = None,
                 sampler: TraceSampler | None = None, telemetry: Telemetry | None = None):
        self.t = telemetry or Telemetry(registry)
        self.logger, self.exporter, self.sampler = logger, exporter, sampler or TraceSampler()

    def on_complete(self, ctx) -> None:
        try:
            self._observe(ctx)
        except Exception:
            pass  # telemetry must never affect the answer

    def _observe(self, ctx) -> None:
        import time
        tenant = ctx.identity.tenant_id
        outcome, route = ctx.outcome, ctx.route or "none"
        total_ms = (time.monotonic() - ctx.started_at) * 1000
        mc = ctx.model_call or {}
        self.t.requests.inc(tenant=tenant, outcome=outcome, route=route)
        self.t.latency.observe(total_ms, tenant=tenant)
        if ctx.cache_result in ("hit", "miss"):
            self.t.cache.inc(tenant=tenant, result=ctx.cache_result)
        for st in ctx.timings:
            self.t.stage.observe(st.ms, stage=st.stage)
            if st.outcome not in ("ok", "skipped"):
                self.t.stage_fail.inc(stage=st.stage, outcome=st.outcome)
        ttft = mc.get("ttft_ms")
        if ttft is None:
            ttft = next((st.ms for st in ctx.timings if st.stage == "model_ttft"), None)
        if ttft is not None:
            self.t.ttft.observe(float(ttft), tenant=tenant)
        if mc:
            self.t.tok_in.inc(mc.get("tokens_in") or 0, tenant=tenant)
            self.t.tok_out.inc(mc.get("tokens_out") or 0, tenant=tenant)
            self.t.cost.inc(float(mc.get("cost_usd") or 0), tenant=tenant)
            if mc.get("failover"):
                self.t.failover.inc(tenant=tenant)
        if outcome == "blocked":
            self.t.guard.inc(tenant=tenant)
        if outcome == "escalated" or ctx.router_trace.get("ticket_failed"):
            self.t.escalations.inc(tenant=tenant)
        if outcome in ("none_found", "escalated") and ctx.retrieval_confidence in (None, "none"):
            self.t.none_found.inc(tenant=tenant)

        reason = self.sampler.decide(outcome, total_ms, ctx.trace_id)
        if self.logger is not None:
            self.logger.log(
                "chat.request", level="error" if outcome == "error" else "info", request_id=ctx.request_id,
                trace_id=ctx.trace_id, tenant=tenant, user=ctx.identity.user_id, course=ctx.course_id,
                role=ctx.identity.role, route=route, outcome=outcome, cache=ctx.cache_result or "none",
                total_ms=round(total_ms, 1), ttft_ms=ttft, provider=mc.get("provider"), model=mc.get("model"),
                tokens_in=mc.get("tokens_in"), tokens_out=mc.get("tokens_out"), cost_usd=mc.get("cost_usd"),
                failover=bool(mc.get("failover")), sampled=reason or "no")
        if reason and self.exporter is not None:
            self.exporter.export(self._trace(ctx, tenant, outcome, route, total_ms, reason, mc))

    @staticmethod
    def _trace(ctx, tenant, outcome, route, total_ms, reason, mc) -> Trace:
        spans, cursor = [], 0.0
        for st in ctx.timings:
            spans.append(Span(st.stage, round(cursor, 1), round(st.ms, 1), "ok" if st.outcome in ("ok", "skipped") else st.outcome))
            cursor += st.ms
        root = Span("chat.request", 0.0, round(total_ms, 1), "error" if outcome == "error" else "ok",
                    {"route": route, "outcome": outcome, "cache": ctx.cache_result or "none", "provider": mc.get("provider"),
                     "model": mc.get("model"), "tokens_in": mc.get("tokens_in"), "tokens_out": mc.get("tokens_out"),
                     "cost_usd": mc.get("cost_usd"), "chunks": len(ctx.chunks)})
        return Trace(ctx.trace_id, ctx.request_id, tenant, outcome, round(total_ms, 1), reason, [root] + spans)


def chain(*hooks: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Run several on_complete hooks in order; one failing never stops the others. Async hooks are awaited."""
    async def run(ctx):
        for h in hooks:
            try:
                r = h(ctx)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                pass
    return run
