"""Turns component `on_event` dicts (guardrails, retriever, vector store, ingestion, escalation) into counters and logs."""
from __future__ import annotations

from .metrics import Registry
from .request import Telemetry
from .scrub import JsonLogger

_VIOLATIONS = {"isolation_violation", "filter_violation"}


class EventSink:
    """`sink = EventSink(...)`; pass `sink` as the `on_event` of each component."""

    def __init__(self, registry: Registry, *, logger: JsonLogger | None = None, telemetry: Telemetry | None = None):
        self.t = telemetry or Telemetry(registry)
        self.logger = logger

    def __call__(self, event: dict) -> None:
        try:
            self._handle(event)
        except Exception:
            pass

    def _handle(self, e: dict) -> None:
        source = str(e.get("stage") or str(e.get("event", "unknown")).split(".")[0])
        decision = str(e.get("decision") or str(e.get("event", "unknown")).split(".")[-1])
        self.t.events.inc(source=source, decision=decision)
        if decision in _VIOLATIONS:
            self.t.violations.inc()
        if self.logger is not None:
            level = "error" if decision in _VIOLATIONS else "info"
            self.logger.log(f"{source}.{decision}", level=level, stage=source, decision=decision,
                            rule=e.get("rule_id") or e.get("rule"), priority=e.get("priority"), trigger=e.get("trigger"),
                            ticket=e.get("ticket_id"), version=e.get("version_id"), reason=e.get("code") or e.get("error"))

    def dead_letter(self, event: dict) -> None:
        """Pass as the ingestion worker's `on_dead_letter`."""
        self.t.dead_letters.inc()
        if self.logger is not None:
            self.logger.log("ingest.dead_letter", level="error", version=event.get("version_id"), reason=event.get("reason"))
