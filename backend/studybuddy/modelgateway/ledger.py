"""Per-call records, the usage ledger and budget states (LLD 09 B7 and A4).

`CallRecord` is what gets logged for every model call: ids, token counts, latency, cost, retries, failover flag and
error type. It never contains prompt or answer text (B5, D7).

`BudgetTracker` turns the ledger into the budget states the orchestrator already understands
(ok -> warning -> soft_limit -> hard_limit). LLD 09 A4 names the states and the 80% warning but not the other
cut-offs, so `soft` and `hard` are PLACEHOLDERS to confirm. Counters are approximate near the limit by design:
concurrent requests can slightly overshoot, which the design accepts.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol

from ..pipeline import BudgetState


@dataclass(frozen=True)
class CallRecord:
    logical_model: str
    provider: str | None
    model: str | None
    lane: str
    tenant_id: str
    user_id: str
    trace_id: str
    routing_version: str
    outcome: str  # ok | error | interrupted | cancelled
    error_kind: str | None
    ttft_ms: float | None
    total_ms: float
    tokens_in: int
    tokens_out: int
    tokens_estimated: bool  # True when the provider reported no usage (e.g. a cancelled stream)
    cost_usd: float | None
    attempts: int
    retries: int
    failover: bool

    def summary(self) -> dict:
        """The part safe to keep on the request context: no tenant/user ids, no text."""
        return {"logical_model": self.logical_model, "provider": self.provider, "model": self.model,
                "outcome": self.outcome, "ttft_ms": self.ttft_ms, "total_ms": self.total_ms,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "tokens_estimated": self.tokens_estimated, "cost_usd": self.cost_usd, "attempts": self.attempts,
                "retries": self.retries, "failover": self.failover, "routing_version": self.routing_version}


class UsageLedger(Protocol):
    def record(self, tenant_id: str, user_id: str, tokens_in: int, tokens_out: int, cost_usd: float | None) -> None: ...


class InMemoryUsageLedger:
    """Redis-counter stand-in (A4: Redis is the hot path, the Postgres ledger is the truth and can rebuild it)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._now = now
        self._tenant_month: dict[tuple[str, str], int] = defaultdict(int)
        self._user_day: dict[tuple[str, str, str], int] = defaultdict(int)
        self.cost_usd: dict[str, float] = defaultdict(float)

    def record(self, tenant_id: str, user_id: str, tokens_in: int, tokens_out: int, cost_usd: float | None) -> None:
        if not tenant_id:
            return
        now = self._now()
        total = tokens_in + tokens_out
        self._tenant_month[(tenant_id, now.strftime("%Y-%m"))] += total
        self._user_day[(tenant_id, user_id, now.strftime("%Y-%m-%d"))] += total
        if cost_usd:
            self.cost_usd[tenant_id] += cost_usd

    def tenant_tokens_this_month(self, tenant_id: str) -> int:
        return self._tenant_month[(tenant_id, self._now().strftime("%Y-%m"))]

    def user_tokens_today(self, tenant_id: str, user_id: str) -> int:
        return self._user_day[(tenant_id, user_id, self._now().strftime("%Y-%m-%d"))]


_ORDER = [BudgetState.OK, BudgetState.WARNING, BudgetState.SOFT_LIMIT, BudgetState.HARD_LIMIT]


def worse(a: BudgetState, b: BudgetState) -> BudgetState:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b


class BudgetTracker:
    """State = the worse of the tenant's monthly budget and the user's daily quota."""

    def __init__(self, ledger: InMemoryUsageLedger, *, tenant_monthly_budget: Mapping[str, int],
                 default_tenant_monthly: int | None = None, user_daily_quota: int | None = 50_000,
                 warn: float = 0.8, soft: float = 0.9, hard: float = 1.0):
        self.ledger, self.tenant_budgets = ledger, dict(tenant_monthly_budget)
        self.default_tenant_monthly, self.user_daily_quota = default_tenant_monthly, user_daily_quota
        self.warn, self.soft, self.hard = warn, soft, hard

    def _state(self, used: int, limit: int | None) -> BudgetState:
        if not limit:
            return BudgetState.OK  # no limit configured for this dimension
        fraction = used / limit
        if fraction >= self.hard:
            return BudgetState.HARD_LIMIT
        if fraction >= self.soft:
            return BudgetState.SOFT_LIMIT
        if fraction >= self.warn:
            return BudgetState.WARNING
        return BudgetState.OK

    def state(self, tenant_id: str, user_id: str) -> BudgetState:
        monthly = self.tenant_budgets.get(tenant_id, self.default_tenant_monthly)
        return worse(self._state(self.ledger.tenant_tokens_this_month(tenant_id), monthly),
                     self._state(self.ledger.user_tokens_today(tenant_id, user_id), self.user_daily_quota))
