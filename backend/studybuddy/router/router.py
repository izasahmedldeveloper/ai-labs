"""The router stage (LLD 07): rules -> intent classifier -> complexity. Optional stage: any error means the pipeline's
default route (cheap model + retrieval), except that the safety rules never depend on anything that can fail."""
from __future__ import annotations

from typing import Callable

from ..pipeline import RequestContext
from ..pipeline.types import Route, RouteDecision
from ..retrieval.types import Embedder
from .classifiers import LexiconIntentClassifier
from .complexity import complexity
from .rules import tier1
from .types import Intent, IntentClassifier, RouterConfig


class Router:
    def __init__(self, classifier: IntentClassifier | None = None, *, embedder: Embedder | None = None,
                 config: RouterConfig | None = None):
        self.classifier = classifier if classifier is not None else LexiconIntentClassifier()
        self.embedder, self.cfg = embedder, config or RouterConfig()

    async def _embedding(self, ctx: RequestContext):
        if ctx.question_embedding is None and self.embedder is not None:
            ctx.question_embedding = tuple((await self.embedder.embed([ctx.question]))[0])  # shared with the retriever
        return ctx.question_embedding

    async def route(self, ctx: RequestContext) -> RouteDecision:
        cfg, trace = self.cfg, ctx.router_trace
        trace["router"] = "v1"

        decision = tier1(ctx.question, cfg)  # tier 1 first: cannot fail, no model, no network
        if decision is not None:
            trace.update({"tier": 1, "rule": decision.reason})
            return decision

        verdict = None
        try:
            embedding = await self._embedding(ctx) if self.classifier.needs_embedding else None
            if not self.classifier.needs_embedding or embedding is not None:
                verdict = self.classifier.classify(ctx.question, embedding)
        except Exception:  # classifier unavailable -> rules + complexity only (s6); never block the answer
            trace["classifier_error"] = True
        if verdict is not None:
            trace.update({"tier": 2, "intent": verdict.intent.value, "intent_confidence": round(verdict.confidence, 3),
                          "classifier_version": verdict.version})
            if verdict.intent == Intent.SENSITIVE and verdict.confidence >= cfg.sensitive_threshold:
                return RouteDecision(Route.HUMAN, False, "classifier:sensitive", confidence=verdict.confidence)
            if verdict.intent in (Intent.CHITCHAT, Intent.OUT_OF_SCOPE) and verdict.confidence >= cfg.redirect_threshold:
                return RouteDecision(Route.REDIRECT, False, f"classifier:{verdict.intent.value}",
                                     canned_answer=cfg.redirect_reply, confidence=verdict.confidence)
            if verdict.intent == Intent.UNCLEAR and cfg.unclear_action == "clarify":
                return RouteDecision(Route.REDIRECT, False, "classifier:unclear", canned_answer=cfg.clarify_reply,
                                     confidence=verdict.confidence)

        score = complexity(ctx.question, len(ctx.history))
        trace.update({"tier": 3, "complexity": round(score.score, 3), "signals": list(score.signals)})
        if score.score >= cfg.strong_threshold:
            return RouteDecision(Route.STRONG_LLM, True, "complexity:" + "+".join(score.signals),
                                 confidence=score.score)
        return RouteDecision(Route.CHEAP_LLM, True, "complexity:low", confidence=1.0 - score.score)


class ShadowRouter:
    """Run a candidate router next to the live one and record disagreements; users only ever see the live decision
    (LLD 07 D6). The candidate can never slow down or break a request beyond the stage timeout it shares."""

    def __init__(self, live, candidate, on_compare: Callable[[RequestContext, RouteDecision, RouteDecision | None], None]):
        self.live, self.candidate, self.on_compare = live, candidate, on_compare

    async def route(self, ctx: RequestContext) -> RouteDecision:
        decision = await self.live.route(ctx)
        shadow_ctx = ctx  # same input; the candidate writes into a scratch trace so it cannot pollute the real one
        try:
            scratch = dict(ctx.router_trace)
            ctx.router_trace.clear()
            other = await self.candidate.route(shadow_ctx)
            ctx.router_trace.clear()
            ctx.router_trace.update(scratch)
        except Exception:
            other = None
            ctx.router_trace.clear()
            ctx.router_trace.update(scratch)
        try:
            self.on_compare(ctx, decision, other)
        except Exception:
            pass
        return decision
