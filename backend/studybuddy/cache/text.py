"""Question normalisation and the "must not fuzzy-match" checks (LLD 06 s2, s4)."""
from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"[A-Za-z0-9']+")
_NUMBER = re.compile(r"\d+(?:[.,:/\-]\d+)*")
NEGATIONS = frozenset({"not", "no", "never", "without", "except", "cannot", "neither", "nor", "none", "nothing"})
STOP = frozenset("a an the is are was were be of to in on for and or do does did what how why when where which who "
                 "can i it this that with as at by from about me my you your please tell explain".split())

# Answers to these are about the asker or about "now": never cached (LLD 06 s2). PLACEHOLDER lists: tune on real traffic.
_PERSONAL = re.compile(
    r"\b(my (grade|grades|mark|marks|score|scores|result|results|gpa|attendance|submission|submissions|"
    r"progress|marks|transcript|account|enrol\w*)|am i (passing|failing|enrolled|eligible)|did i (pass|fail|submit)|"
    r"have i (passed|failed|submitted))\b", re.I)
_TIME_SENSITIVE = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|this (week|month|semester|term)|next (week|month)|right now|currently|"
    r"latest|upcoming|how many days|days left|time left|the next (class|lecture|quiz|exam|deadline))\b", re.I)
_DEADLINE_LIKE = re.compile(r"\b(deadline|due|exam|quiz|midterm|final|submit|submission|schedule|office hours)\b", re.I)


def normalize_question(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip("?!. ")


def is_personal(text: str) -> bool:
    return bool(_PERSONAL.search(text))


def is_time_sensitive(text: str) -> bool:
    return bool(_TIME_SENSITIVE.search(text))


def is_deadline_like(text: str) -> bool:
    return bool(_DEADLINE_LIKE.search(text))


@dataclass(frozen=True)
class Signature:
    numbers: tuple[str, ...]
    negations: int
    names: frozenset[str]  # capitalised words that are not the first word (people, products, "BCNF")
    words: frozenset[str]


def signature(text: str) -> Signature:
    tokens = _WORD.findall(text)
    lowered = [t.lower() for t in tokens]
    negs = sum(1 for t in lowered if t in NEGATIONS or t.endswith("n't"))
    names = frozenset(t.lower() for i, t in enumerate(tokens) if i > 0 and (t[0].isupper() and t != "I"))
    return Signature(tuple(sorted(_NUMBER.findall(text))), negs, names, frozenset(lowered))


def same_signature(a: Signature, b: Signature) -> bool:
    """Numbers, negations and names must agree. A name written lower case in the other question still counts."""
    return (a.numbers == b.numbers and a.negations == b.negations
            and a.names <= b.words and b.names <= a.words)


def content_overlap(a: Signature, b: Signature) -> float:
    wa, wb = a.words - STOP, b.words - STOP
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
