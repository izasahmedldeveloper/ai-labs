"""Input guardrails (LLD 08 section 3): required stage, 100 ms budget, fails closed.

Two entry points, matching how the orchestrator uses a guardrail stage (LLD 03):
  sanitize(text)  synchronous, runs FIRST: strips invisible characters and redacts PII/secrets, so the cache
                  lookup, the embedding and the model only ever see the redacted question.
  check(ctx)      async decision: limits, hidden-text, known attack patterns, optional classifier.

PII redaction is applied by sanitize(), so check() reports `allow_with_redactions` with `text=None`
("redactions already applied") and the orchestrator does not restart the cache lookup.
Only rule ids, categories and counts are ever emitted for audit, never the question text.
"""
from __future__ import annotations

from typing import Callable

from ..pipeline import GuardAction, GuardrailDecision, RequestContext
from .classifiers import TextClassifier
from .policy import ENFORCE, OFF, RAW, SKELETON, SQUASHED, Policy, Rule
from .text import fold_fullwidth, redact_pii, redact_secrets, skeleton, squash, strip_invisible

AuditSink = Callable[[dict], None]


class RuleInputGuardrails:
    def __init__(self, policy: Policy | None = None, *, classifier: TextClassifier | None = None,
                 on_event: AuditSink | None = None):
        self.policy = policy or Policy()
        self.classifier = classifier
        self.on_event = on_event

    @property
    def policy_version(self) -> str:
        return self.policy.version

    # ------------------------------------------------------------------------------ sanitize (sync, first)
    def sanitize(self, text: str) -> str:
        clean, _, _ = strip_invisible(fold_fullwidth(text))
        clean, _ = redact_secrets(clean, self._enforced_secret_patterns())
        clean, _ = redact_pii(clean, self.policy.extra_pii)
        return clean

    def _enforced_secret_patterns(self):
        return [r.pattern for r in self.policy.secret_rules if r.mode == ENFORCE]

    # ------------------------------------------------------------------------------ check (async decision)
    async def check(self, ctx: RequestContext) -> GuardrailDecision:
        p = self.policy
        question = ctx.question  # already sanitized when run by the orchestrator
        original = ctx.original_question if ctx.original_question is not None else question

        if not question.strip():
            return self._block(ctx, "limit.empty", "limit")
        if len(question) > p.max_question_chars:
            return self._block(ctx, "limit.length", "limit")

        _, hidden, tag_chars = strip_invisible(fold_fullwidth(original))
        if tag_chars and p.block_hidden_text:
            return self._block(ctx, "limit.hidden_text", "injection")

        views = {RAW: question}
        skel = skeleton(question)
        views[SKELETON] = skel
        views[SQUASHED] = squash(skel)
        for rule in p.input_rules:
            if rule.mode == OFF or not rule.pattern.search(views[rule.target]):
                continue
            if rule.mode == ENFORCE:
                return self._block(ctx, rule.id, rule.category)
            self._emit(ctx, "shadow_hit", rule=rule)  # shadow: logged for review, request continues

        if self.classifier is not None:
            verdict = await self.classifier.classify(question)  # exceptions propagate: fail closed
            if verdict.score >= p.classifier_block_threshold:
                return self._block(ctx, f"classifier.{verdict.label}", "classifier", score=verdict.score)

        redactions = self._redaction_summary(original)
        if redactions:
            self._emit(ctx, "redacted", **redactions)
            return GuardrailDecision(GuardAction.REDACT, "pii_or_secret_redacted")
        return GuardrailDecision(GuardAction.ALLOW)

    # ------------------------------------------------------------------------------ helpers
    def _redaction_summary(self, original: str) -> dict:
        folded, _, _ = strip_invisible(fold_fullwidth(original))
        folded, secrets = redact_secrets(folded, self._enforced_secret_patterns())
        _, kinds = redact_pii(folded, self.policy.extra_pii)
        summary = {}
        if kinds:
            summary["pii_kinds"] = kinds
        if secrets:
            summary["secrets"] = secrets
        return summary

    def _block(self, ctx: RequestContext, rule_id: str, category: str, **extra) -> GuardrailDecision:
        self._emit(ctx, "block", rule_id=rule_id, category=category, **extra)
        return GuardrailDecision(GuardAction.BLOCK, rule_id)

    def _emit(self, ctx: RequestContext, decision: str, rule: Rule | None = None, **fields) -> None:
        if self.on_event is None:
            return
        event = {"stage": "input_guardrails", "decision": decision, "policy_version": self.policy.version,
                 "trace_id": ctx.trace_id}
        if rule is not None:
            event.update(rule_id=rule.id, category=rule.category, mode=rule.mode)
        event.update(fields)
        self.on_event(event)
