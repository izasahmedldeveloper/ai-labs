"""When to escalate and how urgent it is (LLD 10 s1, s3). Priorities and restricted topics are PLACEHOLDERS to be agreed
with the tutoring team; they are configuration, not law."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Trigger(str, Enum):
    SAFETY = "safety"
    SENSITIVE = "sensitive_topic"
    LOW_CONFIDENCE = "low_confidence"
    FAILED_CHECKS = "failed_checks"
    USER_REQUEST = "user_request"
    DEGRADED = "degraded"
    ROUTER = "router"


PRIORITIES = ("low", "normal", "high", "urgent")


@dataclass(frozen=True)
class Plan:
    trigger: Trigger
    priority: str
    restricted: bool
    dedup_key: str


# (pattern on the router reason, trigger, priority, restricted). First match wins. Rule numbers are those in router/rules.py.
DEFAULT_TABLE: tuple[tuple[re.Pattern, Trigger, str, bool], ...] = (
    (re.compile(r"^rule:safety\."), Trigger.SAFETY, "urgent", True),
    (re.compile(r"^rule:escalate\.(1|2|9)$"), Trigger.SENSITIVE, "high", True),   # harassment, legal threat, personal hardship
    (re.compile(r"^rule:escalate\.(3|4|5|6|7|8)$"), Trigger.SENSITIVE, "high", False),  # complaint, grade, deferral, extension
    (re.compile(r"^rule:escalate\."), Trigger.SENSITIVE, "normal", False),
    (re.compile(r"^classifier:sensitive$"), Trigger.SENSITIVE, "normal", False),
    (re.compile(r"^user_request$"), Trigger.USER_REQUEST, "normal", False),
    (re.compile(r"^low_confidence$"), Trigger.LOW_CONFIDENCE, "normal", False),
    (re.compile(r"^failed_checks$"), Trigger.FAILED_CHECKS, "normal", False),
    (re.compile(r"^degraded$"), Trigger.DEGRADED, "normal", False),
)


@dataclass(frozen=True)
class EscalationConfig:
    table: tuple = DEFAULT_TABLE
    sla_hours: dict = field(default_factory=lambda: {"urgent": 1, "high": 4, "normal": 24, "low": 72})  # D2; wall-clock hours, not business hours
    rate_limit_per_hour: int = 5          # per student, all courses
    dedup_window_hours: int = 24
    miss_threshold: int = 2               # D: "student asks 2+ times"; counts the current miss
    miss_lookback_messages: int = 10
    renotify_after_s: dict = field(default_factory=lambda: {"urgent": 900, "high": 3600, "normal": 4 * 3600, "low": 8 * 3600})
    renotifications_per_level: int = 2
    no_response_close_days: int = 7
    oncall_user_ids: tuple[str, ...] = ()  # urgent tickets also alert these people (tenant-configured backups)


def plan_for(reason: str, cfg: EscalationConfig | None = None) -> Plan:
    cfg = cfg or EscalationConfig()
    for pattern, trigger, priority, restricted in cfg.table:
        if pattern.search(reason):
            return Plan(trigger, priority, restricted, reason)
    return Plan(Trigger.ROUTER, "normal", False, reason)  # unknown reason: still a ticket, never dropped
