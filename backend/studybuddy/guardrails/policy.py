"""Versioned guardrail policy (LLD 08 section 6).

A policy is plain, immutable configuration: the rules, their modes and the limits. Its `version` is stamped on
every request context (and later on messages and cache entries, LLD 06 section 6) so a policy change can
invalidate cached answers and any decision can be explained after the fact.

Rule modes (D7 "new rules shadow first"):
  enforce - a hit blocks/redacts
  shadow  - a hit is logged for review but changes nothing (use for any new or noisy rule)
  off     - kill switch. Turning off a SAFETY rule needs admin approval and an audit-log entry; this code only
            implements the switch, the approval process lives outside it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

ENFORCE, SHADOW, OFF = "enforce", "shadow", "off"
RAW, SKELETON, SQUASHED = "raw", "skeleton", "squashed"


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    pattern: re.Pattern
    mode: str = ENFORCE
    target: str = SKELETON  # which copy of the text the pattern runs on


_V = r"(?:ignore|disregard|forget|override|bypass)"
_QUALIFIER = r"(?:your|previous|prior|above|earlier|preceding|initial|original|system|safety)"
_TARGET = r"(?:instructions?|rules|guidelines|prompts?|directions|restrictions|guardrails|programming|training)"


def _r(rule_id: str, category: str, pattern: str, *, mode: str = ENFORCE, target: str = SKELETON, flags: int = 0) -> Rule:
    return Rule(rule_id, category, re.compile(pattern, flags), mode, target)


# Patterns run on the lowercase, look-alike-free skeleton. They are deliberately narrow: "ignore the rules of
# inference" or "print the system call table" are ordinary course questions and must not be blocked (< 1% false blocks).
INPUT_RULES: tuple[Rule, ...] = (
    _r("injection.override", "injection",
       rf"\b{_V}\s+(?:(?:the|these|those)\s+)?(?:{_QUALIFIER}\s+){{1,3}}{_TARGET}\b"),
    _r("injection.override_all", "injection",
       rf"\b{_V}\s+(?:all|any|every)\s+(?:of\s+)?(?:(?:the|{_QUALIFIER})\s+){{0,3}}{_TARGET}\b"),
    _r("injection.forget_everything", "injection",
       r"\bforget\s+(?:everything|all)\s+(?:that\s+)?you\s+(?:know|were told|have been told|learned|were taught)\b"),
    _r("injection.extract_prompt", "injection",
       r"\b(?:reveal|show|print|repeat|display|output|tell|give|leak|dump|share|recite|write)\b[^.\n]{0,30}"
       r"\byour\s+(?:(?:system|hidden|initial|original|secret|internal|full|exact)\s+){1,2}"
       r"(?:prompt|instructions?|rules|message|programming)\b"),
    _r("injection.extract_system_prompt", "injection",
       r"\b(?:reveal|print|repeat|dump|leak|output)\b[^.\n]{0,30}"
       r"\bthe\s+(?:(?:system|hidden|secret|internal)\s+){1,2}(?:prompt|instructions?|message)\b"),
    _r("injection.ask_prompt", "injection",
       r"\bwhat\s+(?:is|are|were)\s+your\s+(?:(?:system|hidden|initial|original|secret|internal)\s+)+"
       r"(?:prompt|instructions?|rules)\b"),
    _r("injection.repeat_above", "injection",
       r"\brepeat\s+(?:everything|all|the\s+text|the\s+words)\s+(?:above|before)\b"),
    _r("injection.persona", "injection",
       r"\b(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are|you\s+will\s+now\s+act\s+as|act\s+as|"
       r"pretend\s+(?:to\s+be|you\s+are|that\s+you\s+are)|role-?play\s+as|simulate)\b[^.\n]{0,60}"
       r"\b(?:dan|jailbroken|unrestricted|unfiltered|uncensored|"
       r"without\s+(?:any\s+)?(?:rules|restrictions|limits|limitations|guidelines|filters)|"
       r"no\s+(?:rules|restrictions|limits|limitations|guidelines|filters|ethics))\b"),
    _r("injection.dan", "injection", r"\bdo\s+anything\s+now\b"),
    _r("injection.dev_mode", "injection",
       r"\byou\s+(?:are|have)\s+(?:now\s+)?(?:in\s+|entered\s+|enabled\s+)?"
       r"(?:developer|dev|god|jailbreak|unrestricted)\s+mode\b"),
    _r("injection.delimiter_spoof", "injection",
       r"(?:<\|?/?(?:im_start|im_end|system|endoftext)\|?>|\[/?inst\]|<</?sys>>)"),
    # The same phrases with every space/punctuation removed: catches "i g n o r e  y o u r  r u l e s".
    _r("injection.squashed", "injection",
       r"(?:ignore(?:all)?(?:the)?(?:previous|prior|above|your)(?:instructions|rules|prompt)|"
       r"disregardyour(?:instructions|rules)|reveal(?:your|the)systemprompt|showyoursystemprompt|"
       r"printyoursystemprompt|doanythingnow|youarenowdan)", target=SQUASHED),
)

# Secrets: matched on the RAW text (case matters for keys). Used on input (redact) and output (block).
SECRET_RULES: tuple[Rule, ...] = (
    _r("secret.aws_key", "secret", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", target=RAW),
    _r("secret.api_key", "secret", r"\bsk-[A-Za-z0-9_-]{20,}", target=RAW),
    _r("secret.github_token", "secret", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", target=RAW),
    _r("secret.private_key", "secret", r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----", target=RAW),
    _r("secret.jwt", "secret", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", target=RAW),
    # Shadow first (D7): "password = input()" in a programming answer would otherwise be a false block.
    _r("secret.assignment", "secret",
       r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{6,}",
       mode=SHADOW, target=RAW, flags=re.IGNORECASE),
)


@dataclass(frozen=True)
class Policy:
    version: str = "2026-10-11.1"
    max_question_chars: int = 2000  # LLD 08 D1
    block_hidden_text: bool = True  # unicode tag characters (hidden instructions) -> block
    input_rules: tuple[Rule, ...] = INPUT_RULES
    secret_rules: tuple[Rule, ...] = SECRET_RULES
    output_rules: tuple[Rule, ...] = ()  # tenant/platform banned patterns for answers, matched on the raw chunk
    extra_pii: tuple[tuple[str, re.Pattern], ...] = ()  # e.g. (("[student-id]", re.compile(r"\bS\d{7}\b")),)
    canary: str | None = None  # secret marker planted in the system prompt; seeing it in an answer = prompt leak
    classifier_block_threshold: float = 0.9
    groundedness_threshold: float = 0.4
    # Optional citation check. The pattern needs exactly one capture group: the cited chunk id, e.g. r"\[(c\d+)\]".
    # Off by default until the model stage (LLD 09) fixes how answers cite sources.
    citation_pattern: re.Pattern | None = None
    require_citations: bool = False

    def with_rule_mode(self, rule_id: str, mode: str) -> "Policy":
        """A copy of this policy with one rule switched to enforce/shadow/off (raises if the rule is unknown)."""
        if mode not in (ENFORCE, SHADOW, OFF):
            raise ValueError(f"unknown mode {mode!r}")
        known = False
        changed = {}
        for name in ("input_rules", "secret_rules", "output_rules"):
            rules = []
            for rule in getattr(self, name):
                if rule.id == rule_id:
                    known = True
                    rule = replace(rule, mode=mode)
                rules.append(rule)
            changed[name] = tuple(rules)
        if not known:
            raise KeyError(rule_id)
        return replace(self, **changed)

    def with_version(self, version: str) -> "Policy":
        return replace(self, version=version)
