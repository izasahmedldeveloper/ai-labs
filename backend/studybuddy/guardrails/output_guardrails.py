"""Output guardrails (LLD 08 section 5): required stage, chunked, fails closed.

check_chunk  runs on every sentence-sized chunk BEFORE it is sent (LLD 03 option C). Fast layers only:
             secrets / canary / banned patterns -> block, PII -> redact, optional policy classifier -> block.
final_ok     runs once on the whole answer after streaming: catches leaks that straddled a chunk boundary,
             validates citations (if the policy defines a citation format) and scores groundedness.
             A False result cannot unsend streamed text; the orchestrator appends a notice and skips the cache.

Mid-stream groundedness sampling is intentionally not done yet: with only a lexical baseline scorer it would
block good answers. Add it when a real entailment model exists (LLD 08 section 5).
"""
from __future__ import annotations

from typing import Callable, Sequence

from ..pipeline import Chunk, ChunkAction, ChunkDecision, RequestContext
from .classifiers import GroundednessScorer, LexicalGroundedness, TextClassifier
from .policy import ENFORCE, OFF, Policy, Rule
from .text import redact_pii

AuditSink = Callable[[dict], None]


class RuleOutputGuardrails:
    def __init__(self, policy: Policy | None = None, *, classifier: TextClassifier | None = None,
                 groundedness: GroundednessScorer | None = None, on_event: AuditSink | None = None):
        self.policy = policy or Policy()
        self.classifier = classifier
        self.groundedness = groundedness or LexicalGroundedness()
        self.on_event = on_event

    @property
    def policy_version(self) -> str:
        return self.policy.version

    # ------------------------------------------------------------------------------ per chunk
    async def check_chunk(self, ctx: RequestContext, text: str, first: bool) -> ChunkDecision:
        p = self.policy
        hit = self._rule_hit(text)
        if hit is not None:
            self._emit(ctx, "block", rule=hit)
            return ChunkDecision(ChunkAction.BLOCK)
        if p.canary and p.canary in text:
            self._emit(ctx, "block", rule_id="leak.canary", category="prompt_leak")
            return ChunkDecision(ChunkAction.BLOCK)
        if self.classifier is not None:
            verdict = await self.classifier.classify(text)  # exceptions propagate: fail closed
            if verdict.score >= p.classifier_block_threshold:
                self._emit(ctx, "block", rule_id=f"classifier.{verdict.label}", category="classifier",
                           score=verdict.score)
                return ChunkDecision(ChunkAction.BLOCK)
        redacted, kinds = redact_pii(text, p.extra_pii)
        if kinds:
            self._emit(ctx, "redact", pii_kinds=kinds)
            return ChunkDecision(ChunkAction.REDACT, redacted)
        return ChunkDecision(ChunkAction.PASS)

    # ------------------------------------------------------------------------------ whole answer
    async def final_ok(self, ctx: RequestContext, answer: str, chunks: Sequence[Chunk]) -> bool:
        p = self.policy
        hit = self._rule_hit(answer)
        if hit is not None or (p.canary and p.canary in answer) or redact_pii(answer, p.extra_pii)[1]:
            # something sensitive straddled a chunk boundary and slipped past the per-chunk check
            self._emit(ctx, "final_fail", rule_id=hit.id if hit else "leak.straddled", category="leak")
            return False

        if p.citation_pattern is not None:
            cited = set(p.citation_pattern.findall(answer))
            known = {c.id for c in chunks}
            if cited - known:
                self._emit(ctx, "final_fail", rule_id="citation.invalid", category="citation")
                return False
            if p.require_citations and chunks and not cited:
                self._emit(ctx, "final_fail", rule_id="citation.missing", category="citation")
                return False

        if chunks:
            score = await self.groundedness.score(answer, chunks)
            ctx.usage["groundedness"] = round(score, 3)
            if score < p.groundedness_threshold:
                self._emit(ctx, "final_fail", rule_id="groundedness.low", category="groundedness", score=score)
                return False
        return True

    # ------------------------------------------------------------------------------ helpers
    def _rule_hit(self, text: str) -> Rule | None:
        """First enforced secret/banned-pattern rule matching the raw text; shadow hits are logged, not returned."""
        for rule in self.policy.secret_rules + self.policy.output_rules:
            if rule.mode == OFF or not rule.pattern.search(text):
                continue
            if rule.mode == ENFORCE:
                return rule
            if self.on_event is not None:
                self.on_event({"stage": "output_guardrails", "decision": "shadow_hit", "rule_id": rule.id,
                               "category": rule.category, "mode": rule.mode, "policy_version": self.policy.version})
        return None

    def _emit(self, ctx: RequestContext, decision: str, rule: Rule | None = None, **fields) -> None:
        if self.on_event is None:
            return
        event = {"stage": "output_guardrails", "decision": decision, "policy_version": self.policy.version,
                 "trace_id": ctx.trace_id}
        if rule is not None:
            event.update(rule_id=rule.id, category=rule.category, mode=rule.mode)
        event.update(fields)
        self.on_event(event)
