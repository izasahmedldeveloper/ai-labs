"""Eval regression gate for CI (LLD 11 s9): compare a run's scores with the stored baseline and fail on a drop."""
from __future__ import annotations

from dataclasses import dataclass

# metric -> (direction, allowed change). "higher" means a drop is bad; "lower" means a rise is bad.
DEFAULT_RULES: dict[str, tuple[str, float]] = {
    "faithfulness": ("higher", 0.03),
    "answer_relevance": ("higher", 0.03),
    "retrieval_recall": ("higher", 0.03),
    "citation_accuracy": ("higher", 0.03),
    "refusal_rate_on_safe": ("lower", 0.02),
    "leak_rate": ("lower", 0.0),            # any new leak fails
    "p95_latency_ms": ("lower", 500.0),
    "cost_per_request_usd": ("lower", 0.002),
}


@dataclass
class GateResult:
    passed: bool
    regressions: list[str]
    missing: list[str]


def compare_to_baseline(current: dict, baseline: dict, rules: dict | None = None) -> GateResult:
    """A metric missing from the current run counts as a failure (a silent gap must not pass the gate)."""
    rules = rules or DEFAULT_RULES
    bad, missing = [], []
    for name, (direction, tol) in rules.items():
        if name not in baseline:
            continue
        if name not in current or current[name] is None:
            missing.append(name)
            continue
        delta = current[name] - baseline[name]
        if (direction == "higher" and delta < -tol - 1e-12) or (direction == "lower" and delta > tol + 1e-12):
            bad.append(f"{name}: {baseline[name]} -> {current[name]} (allowed {direction} by {tol})")
    return GateResult(not bad and not missing, bad, missing)
