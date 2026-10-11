"""Small text helpers shared by selection, the in-memory store and the fakes."""
from __future__ import annotations

import re
from typing import Mapping

STOPWORDS = frozenset(
    "a an and are as at be but by can could do does did for from had has have how i in into is it its me my of on "
    "or our so than that the their them then there these they this those to was we were what when where which who "
    "why will with would you your about after before between each any all some such not no yes also very more most "
    "other only over under".split())
_WORD = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def content_tokens(text: str, synonyms: Mapping[str, str] | None = None) -> list[str]:
    """Lowercase content words, stopwords removed, lightly stemmed; `synonyms` maps a word to its canonical form."""
    out = []
    for word in _WORD.findall(text.lower()):
        if word in STOPWORDS or len(word) < 2:
            continue
        if synonyms:
            word = synonyms.get(word, word)
        out.append(stem(word))
    return out


def lexical_coverage(query: str, text: str) -> float:
    """Share of the query's content words that appear in the text (0..1). Store-independent relevance signal."""
    wanted = set(content_tokens(query))
    if not wanted:
        return 0.0
    return len(wanted & set(content_tokens(text))) / len(wanted)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
