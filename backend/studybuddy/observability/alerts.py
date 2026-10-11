"""SLOs and alert rules (LLD 11 s6-s7). Rules read the metrics registry; the engine reports what is firing and what changed."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .metrics import Registry
from .request import GOOD_OUTCOMES, Telemetry

AVAILABILITY_SLO = 0.995
TTFT_P95_MS = 1500
TOTAL_P95_MS = 6000


@dataclass(frozen=True)
class Alert:
    name: str
    severity: str  # page | ticket
    message: str
    value: float | None = None
    threshold: float | None = None


@dataclass
class Thresholds:
    availability_slo: float = AVAILABILITY_SLO
    fast_window_s: int = 300
    slow_window_s: int = 3600
    burn_rate: float = 14.4          # 2% of a 30-day budget in one hour: page
    min_requests: int = 20           # below this a ratio means nothing
    latency_window_s: int = 600
    ttft_p95_ms: float = TTFT_P95_MS
    total_p95_ms: float = TOTAL_P95_MS
    min_latency_samples: int = 20
    spend_window_s: int = 3600
    spend_baseline_factor: float = 3.0   # this hour's spend vs the average of the previous hours
    block_window_s: int = 600
    block_ratio: float = 0.25
    urgent_unclaimed_max: int = 0
    failover_ratio: float = 0.5


def _error_ratio(t: Telemetry, window: int) -> tuple[float, float]:
    total = t.requests.window(window)
    bad = total - sum(t.requests.window(window, outcome=o) for o in GOOD_OUTCOMES) - t.requests.window(window, outcome="busy")
    counted = total - t.requests.window(window, outcome="busy")
    return (bad / counted if counted else 0.0), counted


class AlertEngine:
    def __init__(self, registry: Registry, telemetry: Telemetry | None = None, thresholds: Thresholds | None = None,
                 notify: Callable[[Alert, str], None] | None = None):
        self.r, self.t, self.th = registry, telemetry or Telemetry(registry), thresholds or Thresholds()
        self.notify = notify
        self._active: dict[str, Alert] = {}
        self.history: list[tuple[str, str]] = []  # (alert name, "firing" | "resolved")

    # -- rules ---------------------------------------------------------------------------------------------------
    def check(self) -> list[Alert]:
        th, t = self.th, self.t
        out: list[Alert] = []
        budget = 1 - th.availability_slo
        fast, n_fast = _error_ratio(t, th.fast_window_s)
        slow, n_slow = _error_ratio(t, th.slow_window_s)
        # Multi-window burn: both windows must burn, so a blip that already ended does not page.
        if n_fast >= th.min_requests and fast / budget >= th.burn_rate and slow / budget >= th.burn_rate:
            out.append(Alert("availability_burn", "page", "Error budget is burning fast", round(fast / budget, 2), th.burn_rate))
        for hist, limit, name in ((t.ttft, th.ttft_p95_ms, "ttft_p95_high"), (t.latency, th.total_p95_ms, "latency_p95_high")):
            n = len([1 for s in hist._recent(th.latency_window_s)])
            p95 = hist.quantile(0.95, th.latency_window_s) if n >= th.min_latency_samples else None
            if p95 is not None and p95 > limit:
                out.append(Alert(name, "ticket", f"P95 {name.split('_')[0]} above target", round(p95, 1), limit))
        if t.violations.value() > 0:
            out.append(Alert("cross_tenant_violation", "page", "Data from another tenant was seen where it must not be",
                             t.violations.value(), 0))
        if t.pii_leaks.value() > 0:
            out.append(Alert("pii_in_logs", "page", "PII found in logs or traces", t.pii_leaks.value(), 0))
        if t.dead_letters.window(th.fast_window_s) > 0:
            out.append(Alert("ingest_dead_letter", "ticket", "A document landed in the dead-letter topic",
                             t.dead_letters.window(th.fast_window_s), 0))
        if t.urgent_unclaimed.value() > th.urgent_unclaimed_max:
            out.append(Alert("urgent_ticket_unclaimed", "page", "Urgent tickets are waiting for a tutor",
                             t.urgent_unclaimed.value(), th.urgent_unclaimed_max))
        spend_now = t.cost.window(th.spend_window_s)
        before = t.cost.window(th.spend_window_s * 4) - spend_now
        baseline = before / 3 if before > 0 else 0
        if baseline > 0 and spend_now > baseline * th.spend_baseline_factor:
            out.append(Alert("spend_anomaly", "ticket", "Spend is far above the recent hourly average", round(spend_now, 4),
                             round(baseline * th.spend_baseline_factor, 4)))
        total = t.requests.window(th.block_window_s)
        if total >= th.min_requests and t.guard.window(th.block_window_s) / total >= th.block_ratio:
            out.append(Alert("guardrail_block_spike", "ticket", "Unusually many requests blocked by guardrails",
                             round(t.guard.window(th.block_window_s) / total, 3), th.block_ratio))
        calls = t.failover.window(th.fast_window_s)
        if calls >= th.min_requests / 2 and calls / max(t.requests.window(th.fast_window_s), 1) >= th.failover_ratio:
            out.append(Alert("provider_failover", "ticket", "Most calls are served by the fallback provider",
                             calls, th.failover_ratio))
        if t.canary.window(th.fast_window_s, result="fail") > 0:
            out.append(Alert("canary_failing", "page", "The end-to-end canary failed", t.canary.window(th.fast_window_s, result="fail"), 0))
        return out

    def evaluate(self) -> list[Alert]:
        """Run the rules, notify on state changes only (firing once, resolved once), return what is firing now."""
        now = {a.name: a for a in self.check()}
        for name, a in now.items():
            if name not in self._active:
                self.history.append((name, "firing"))
                self._send(a, "firing")
        for name, a in list(self._active.items()):
            if name not in now:
                self.history.append((name, "resolved"))
                self._send(a, "resolved")
        self._active = now
        return list(now.values())

    def _send(self, alert: Alert, state: str) -> None:
        if self.notify is not None:
            try:
                self.notify(alert, state)
            except Exception:
                pass


def slis(t: Telemetry, window_s: int = 3600) -> dict:
    """Current service level indicators (for a status page or the tenant view)."""
    err, n = _error_ratio(t, window_s)
    return {"requests": n, "availability": None if not n else round(1 - err, 5),
            "ttft_p95_ms": t.ttft.quantile(0.95, window_s), "total_p95_ms": t.latency.quantile(0.95, window_s)}
