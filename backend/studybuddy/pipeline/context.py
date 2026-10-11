"""Request context (LLD 03 section 2): one object passed through every stage."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BudgetState(str, Enum):
    OK = "ok"
    WARNING = "warning"
    SOFT_LIMIT = "soft_limit"  # only the cheap model tier may be used (LLD 09 A4)
    HARD_LIMIT = "hard_limit"  # requests are refused


@dataclass(frozen=True)
class Identity:
    """From the verified JWT (via trusted gateway headers) only. Never changes during a request."""

    tenant_id: str
    user_id: str
    role: str


@dataclass
class StageTiming:
    stage: str
    outcome: str  # ok | skipped | timeout | error | failed
    ms: float


_LOCKED = ("identity", "course_id", "request_id", "trace_id")


@dataclass
class RequestContext:
    identity: Identity
    course_id: str
    question: str
    request_id: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    budget_state: BudgetState = BudgetState.OK
    started_at: float = field(default_factory=time.monotonic)

    # --- results so far: each stage writes its own findings, later stages read them ---
    original_question: str | None = None
    guardrail_action: str | None = None
    policy_version: str | None = None  # guardrail policy in force; stamped on messages and cache entries (LLD 06 s6)
    cache_result: str | None = None  # hit | miss | skipped
    route: str | None = None
    route_reason: str | None = None
    retrieval_confidence: str | None = None
    chunks: tuple = ()
    answer: str = ""
    outcome: str = "pending"  # ok | blocked | none_found | error | escalated | cancelled | busy
    cacheable: bool = True
    notices: list[str] = field(default_factory=list)
    timings: list[StageTiming] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name: str, value: Any) -> None:
        # Identity, scope and ids are set once: a stage cannot change who the request is for.
        if name in _LOCKED and name in self.__dict__:
            raise AttributeError(f"{name} is read-only after the request context is created")
        object.__setattr__(self, name, value)
