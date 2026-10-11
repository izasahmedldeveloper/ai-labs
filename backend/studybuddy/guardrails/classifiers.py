"""Plug-in points for the model-based layers of LLD 08 (small classifiers, entailment/groundedness).

The rule layers work on their own. These protocols are where the small injection/policy classifier (D3) and an
NLI groundedness model go later; the stages call them AFTER the cheap rules, and any exception they raise
propagates so the orchestrator fails closed (a broken safety check never means "skip the check").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from ..pipeline import Chunk


@dataclass(frozen=True)
class ClassifierVerdict:
    label: str  # e.g. "injection", "self_harm", "harassment", "ok"
    score: float  # 0..1, higher = more likely the labelled problem


class TextClassifier(Protocol):
    async def classify(self, text: str) -> ClassifierVerdict: ...


class GroundednessScorer(Protocol):
    async def score(self, answer: str, chunks: Sequence[Chunk]) -> float:
        """0..1: how much of the answer is supported by the retrieved chunks."""
        ...


_STOPWORDS = frozenset(
    "this that these those with from have has had been being were was are and the for not but you your they them "
    "their there then than into onto over under also very more most some such only other when where which while "
    "what will would should could can may might must about after before because between each both any all its "
    "how why who whom".split())
_TOKEN = re.compile(r"[a-z0-9]{4,}")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _content_tokens(text: str) -> list[str]:
    return [_stem(w) for w in _TOKEN.findall(text.casefold()) if w not in _STOPWORDS]


class LexicalGroundedness:
    """Baseline groundedness: what share of each sentence's content words appear in the retrieved chunks.

    This is a stand-in for the small NLI/entailment model in LLD 08 section 5: it catches an answer that wanders
    off into topics the notes never mention, but it cannot tell a paraphrase from a contradiction. Treat the
    score as a coarse quality signal, not proof of truth, and replace it with an NLI scorer when one exists.
    """

    async def score(self, answer: str, chunks: Sequence[Chunk]) -> float:
        vocabulary = set()
        for chunk in chunks:
            vocabulary.update(_content_tokens(chunk.text))
        scores = []
        for sentence in _SENTENCE.split(answer):
            tokens = _content_tokens(sentence)
            if len(tokens) < 2:
                continue
            scores.append(sum(1 for t in tokens if t in vocabulary) / len(tokens))
        return sum(scores) / len(scores) if scores else 1.0
