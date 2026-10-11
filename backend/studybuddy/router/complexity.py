"""Tier 3: question complexity without an LLM (LLD 07 s5). Default is cheap; strong only above the threshold.

The signal weights are PLACEHOLDERS (no labelled data yet). They are additive and capped at 1.0 so a long but simple
question does not automatically become expensive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .rules import prep

_REASONING = re.compile(r"\b(derive|prove|proof|why|explain why|justify|analy[sz]e|evaluate|critique|design|optimi[sz]e|"
                        r"trade ?offs?|step by step|walk me through)\b")
_COMPARE = re.compile(r"\b(compare|contrast|difference between|differences between|versus|vs|pros and cons|better than)\b")
_MATH_CODE = re.compile(r"(\d\s*[\+\-\*/\^=]\s*\d|\b(sql|select|join|query|algorithm|complexity|big o|code|function|"
                        r"integral|derivative|matrix|equation|theorem)\b|`|\bo\(n)")
_MULTI = re.compile(r"\b(and then|and also|as well as|first|second|third|finally)\b|\?.*\?")


@dataclass(frozen=True)
class Complexity:
    score: float
    signals: tuple[str, ...]


def complexity(text: str, history_len: int = 0) -> Complexity:
    t = prep(text)
    score, signals = 0.0, []
    words = len(t.split())
    if words > 30:
        score += 0.30; signals.append("long")
    elif words > 18:
        score += 0.15; signals.append("medium_length")
    if _REASONING.search(t):
        score += 0.35; signals.append("reasoning")
    if _COMPARE.search(t):
        score += 0.30; signals.append("compare")
    if _MATH_CODE.search(text.lower()):
        score += 0.20; signals.append("math_or_code")
    if _MULTI.search(text.lower()) or len(re.findall(r"\band\b", t)) >= 2:
        score += 0.20; signals.append("multi_part")
    if history_len >= 6:
        score += 0.10; signals.append("deep_conversation")
    return Complexity(min(score, 1.0), tuple(signals))
