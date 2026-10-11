"""Structured, PII-safe logging (LLD 11 s5): allow-listed fields only, text never logged, string values scrubbed."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from ..guardrails.text import redact_pii

# Student and tutor text must never appear in logs (LLD 11 s5): refuse these keys outright.
FORBIDDEN_KEYS = frozenset({
    "question", "answer", "text", "content", "body", "message", "prompt", "completion", "reply", "query",
    "original_question", "rewritten_query", "email", "name", "password", "token", "authorization", "secret",
})
ALLOWED_KEYS = frozenset({
    "event", "ts", "level", "request_id", "trace_id", "tenant", "user", "course", "role", "route", "outcome",
    "cache", "provider", "model", "logical_model", "ms", "ttft_ms", "total_ms", "tokens_in", "tokens_out", "cost_usd",
    "stage", "decision", "rule", "count", "attempts", "failover", "error", "error_type", "sampled", "reason",
    "ticket", "priority", "trigger", "document", "version", "status", "topic", "service", "alert", "severity",
    "value", "threshold",
})
_LONG = re.compile(r"[A-Za-z0-9_\-]{40,}")  # key/token-looking runs
_MAX = 300


def scrub_value(v: Any) -> Any:
    if isinstance(v, str):
        red, _ = redact_pii(v)
        red = _LONG.sub("[REDACTED]", red)
        return red[:_MAX]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [scrub_value(x) for x in list(v)[:20]]
    return scrub_value(str(v))


def make_record(event: str, **fields) -> dict:
    """Build one log record. Forbidden keys raise (a programming error caught in tests); unknown keys are dropped."""
    bad = FORBIDDEN_KEYS & set(fields)
    if bad:
        raise ValueError(f"log field(s) not allowed: {sorted(bad)}")
    rec = {k: scrub_value(v) for k, v in fields.items() if k in ALLOWED_KEYS}
    rec["event"] = event
    return rec


class JsonLogger:
    """Writes one JSON line per event to a `logging.Logger` (or any sink callable)."""

    def __init__(self, sink: Callable[[str], None] | logging.Logger | None = None, clock: Callable[[], float] = time.time,
                 service: str = "studybuddy"):
        if sink is None:
            sink = logging.getLogger("studybuddy.events")
        self._emit = sink.info if isinstance(sink, logging.Logger) else sink
        self.clock, self.service = clock, service

    def log(self, event: str, level: str = "info", **fields) -> dict:
        rec = make_record(event, **fields)
        rec.update(ts=round(self.clock(), 3), level=level, service=self.service)
        try:
            self._emit(json.dumps(rec, sort_keys=True, default=str))
        except Exception:
            pass  # logging must never break a request
        return rec
