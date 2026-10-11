"""Tier 2 intent classifiers (LLD 07 s4).

`LexiconIntentClassifier` is a transparent BASELINE, not the trained model the design describes: word lists for
chitchat, off-topic subjects and softer sensitive signals. `CentroidIntentClassifier` is a small trainable classifier over
the EXISTING question embedding (nearest class centroid), versioned by a hash of its training data. Neither has been
evaluated on real student questions; both only prove the plumbing. Replace with logistic regression / boosting trained
on labelled traffic when it exists.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from typing import Iterable, Sequence

from .rules import prep
from .types import Intent, IntentVerdict

_CHITCHAT = re.compile(r"\b(how are you|how's it going|how is it going|what's up|whats up|tell me a joke|"
                       r"are you (a )?(robot|bot|human|real|ai)|what's your name|do you like|favourite|favorite|"
                       r"i'm bored|im bored|you are (great|awesome|funny|stupid)|good job|nice one)\b")
_OFF_TOPIC = re.compile(r"\b(football|soccer|cricket score|world cup|nba|movie|netflix|recipe|weather|stock price|bitcoin|"
                        r"crypto|horoscope|celebrity|song lyrics|joke|dating|girlfriend|boyfriend|election|president of|"
                        r"capital of|where to buy|restaurant|holiday|vacation|lottery|gamble|betting)\b")
_SOFT_SENSITIVE = re.compile(r"\b(stressed|overwhelmed|anxious|anxiety|depressed|can't cope|cant cope|burnt out|burned out|"
                             r"panic|struggling (a lot|badly)|give up on|failing the course|i'm failing|im failing|"
                             r"unfair|cheating accusation|accused of|plagiaris\w+ (case|accusation)|disability|"
                             r"accommodation|mental health|counsell?ing)\b")


class LexiconIntentClassifier:
    version = "lexicon-0"
    needs_embedding = False

    def classify(self, text: str, embedding: Sequence[float] | None = None) -> IntentVerdict:
        t = prep(text)
        words = t.split()
        if _SOFT_SENSITIVE.search(t):
            return IntentVerdict(Intent.SENSITIVE, 0.7, self.version)
        if _OFF_TOPIC.search(t):
            return IntentVerdict(Intent.OUT_OF_SCOPE, 0.8, self.version)
        if _CHITCHAT.search(t):
            return IntentVerdict(Intent.CHITCHAT, 0.8, self.version)
        if len(words) < 2:
            return IntentVerdict(Intent.UNCLEAR, 0.5, self.version)
        return IntentVerdict(Intent.COURSE_QUESTION, 0.6, self.version)


def _unit(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


class CentroidIntentClassifier:
    """Nearest-centroid over unit-length embeddings. Confidence = softmax over cosine scores (temperature 0.1)."""

    needs_embedding = True

    def __init__(self, centroids: dict[Intent, list[float]], version: str):
        self.centroids, self.version = centroids, version

    @classmethod
    def fit(cls, examples: Iterable[tuple[Intent, Sequence[float]]]) -> "CentroidIntentClassifier":
        sums: dict[Intent, list[float]] = {}
        counts: dict[Intent, int] = defaultdict(int)
        material = hashlib.sha256()
        for intent, vec in examples:
            unit = _unit(vec)
            acc = sums.setdefault(intent, [0.0] * len(unit))
            for i, x in enumerate(unit):
                acc[i] += x
            counts[intent] += 1
            material.update(intent.value.encode() + repr([round(x, 6) for x in unit]).encode())
        if len(sums) < 2:
            raise ValueError("need examples of at least two intents")
        return cls({i: _unit(v) for i, v in sums.items()}, f"centroid-{material.hexdigest()[:10]}")

    def classify(self, text: str, embedding: Sequence[float] | None) -> IntentVerdict:
        if embedding is None:
            raise ValueError("this classifier needs the question embedding")
        q = _unit(embedding)
        scores = {i: sum(a * b for a, b in zip(q, c)) for i, c in self.centroids.items()}
        top = max(scores, key=scores.get)
        exps = {i: math.exp((s - scores[top]) / 0.1) for i, s in scores.items()}
        return IntentVerdict(top, exps[top] / sum(exps.values()), self.version)
