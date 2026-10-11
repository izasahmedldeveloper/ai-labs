"""Deterministic text hygiene for the guardrails (LLD 08 section 3, steps 1-2).

Two different copies of the text exist on purpose:
  * the FORWARDED text (what the cache, retriever and model see): minimal, meaning-preserving changes only
    (fullwidth forms folded, invisible characters stripped, PII and secrets replaced by placeholders);
  * the DETECTION copy ("skeleton"): aggressively normalised (case, accents, look-alike letters, leetspeak,
    spacing tricks) so attack phrases cannot hide behind cosmetic tricks. It is never forwarded, so it can be
    lossy without corrupting a student's real question (e.g. x^2 stays x^2, Hindi text stays Hindi).

All regexes use bounded quantifiers so a hostile 2,000-character input cannot cause catastrophic
backtracking inside the 100 ms input budget.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})  # control, format (zero-width, bidi), surrogate, private use


# ------------------------------------------------------------------------------------ forwarded text
def fold_fullwidth(text: str) -> str:
    """Fold fullwidth ASCII (e.g. digits and @ used to dodge PII rules) to plain ASCII, then NFC.

    NFKC would also turn superscripts into digits (x^2 -> x2), which changes the meaning of maths questions,
    so only the fullwidth block is folded here; the heavier NFKC is reserved for the detection copy.
    """
    out = []
    for ch in text:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def strip_invisible(text: str) -> tuple[str, int, int]:
    """Remove control/format/invisible characters. Returns (clean_text, hidden_count, tag_char_count).

    Unicode "tag" characters (U+E0000 block) render as nothing but can carry a hidden instruction
    ("ASCII smuggling"); they have no legitimate use in a student question, so callers may treat any as hostile.
    """
    out: list[str] = []
    hidden = tags = 0
    for ch in text:
        o = ord(ch)
        if 0xE0000 <= o <= 0xE007F:
            tags += 1
            hidden += 1
            continue
        if ch in "\n\t":
            out.append(ch)
            continue
        if ch in "  ":
            out.append("\n")
            continue
        if ch == "\r":
            continue
        if unicodedata.category(ch) in _INVISIBLE_CATEGORIES or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
            hidden += 1
            continue
        out.append(ch)
    return "".join(out), hidden, tags


# ------------------------------------------------------------------------------------ detection copy
_CONFUSABLES = str.maketrans({
    # Cyrillic look-alikes (lowercase, applied after casefold)
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w",
    "к": "k", "т": "t", "н": "h", "м": "m",
    # Greek look-alikes
    "ο": "o", "ν": "v", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x",
    # leetspeak
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s",
})
_WS = re.compile(r"\s+")
_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def skeleton(text: str) -> str:
    """Lossy, lowercase, look-alike-free form of the text used ONLY for matching attack phrases."""
    t = unicodedata.normalize("NFKC", text)
    t, _, _ = strip_invisible(t)
    t = unicodedata.normalize("NFD", t.casefold())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    t = t.translate(_CONFUSABLES)
    return _WS.sub(" ", t).strip()


def squash(skel: str) -> str:
    """Skeleton with everything but letters and digits removed: catches 'i g n o r e' and 'i.g.n.o.r.e'."""
    return _NOT_ALNUM.sub("", skel)


# ------------------------------------------------------------------------------------ PII
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,4}")
_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")  # separators only BETWEEN digits, never trailing
_AADHAAR = re.compile(r"(?<!\d)\d{4}[ -]\d{4}[ -]\d{4}(?![ -]?\d)")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,5}\)[ .-]?)?(?:\d{10,15}|\d{2,5}(?:[ .-]\d{2,6}){1,4})(?!\w)")
_DATE_PREFIX = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    return "[card]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group()


def _redact_phone(m: re.Match) -> str:
    candidate = m.group()
    digits = re.sub(r"\D", "", candidate)
    if not 10 <= len(digits) <= 15 or _DATE_PREFIX.match(candidate.lstrip("+( ")):
        return candidate
    return "[phone]"


def redact_pii(text: str, extra: Iterable[tuple[str, re.Pattern]] = ()) -> tuple[str, list[str]]:
    """Replace emails, card numbers, national IDs and phone numbers with placeholders.

    `extra` is (placeholder, pattern) pairs for tenant-specific identifiers (e.g. student numbers).
    Returns (redacted_text, kinds_found); kinds are for the audit log (never the matched values).
    Best-effort by nature: it will sometimes redact a long number that is not personal (a safe failure).
    """
    kinds: list[str] = []

    def apply(pattern: re.Pattern, repl, kind: str, current: str) -> str:
        new, count = pattern.subn(repl, current)
        if new != current:
            kinds.append(kind)
        return new

    text = apply(_EMAIL, "[email]", "email", text)
    text = apply(_CARD, _redact_card, "card", text)
    text = apply(_AADHAAR, "[id]", "national_id", text)
    text = apply(_SSN, "[id]", "national_id", text)
    text = apply(_PAN, "[id]", "national_id", text)
    for placeholder, pattern in extra:
        text = apply(pattern, placeholder, "custom_id", text)
    text = apply(_PHONE, _redact_phone, "phone", text)
    return text, sorted(set(kinds))


def redact_secrets(text: str, patterns: Iterable[re.Pattern]) -> tuple[str, int]:
    """Replace API keys, tokens and private keys with [secret]. Returns (text, number_of_secrets_found)."""
    found = 0
    for pattern in patterns:
        text, count = pattern.subn("[secret]", text)
        found += count
    return text, found
