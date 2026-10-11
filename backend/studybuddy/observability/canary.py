"""Synthetic canary (LLD 11 s8): ask real questions through the real pipeline as two test tenants and check the answer.

It proves the whole path works (availability, latency, a citation) and that tenant B's marker never shows up for tenant A.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .request import Telemetry


@dataclass
class CanaryTarget:
    name: str
    ask: Callable[[str], Awaitable[dict]]  # question -> {"answer": str, "citations": list, "outcome": str}
    question: str
    expect_phrase: str       # a fact only this tenant's seeded material contains
    foreign_marker: str      # a string that exists only in the OTHER canary tenant's material


@dataclass
class CanaryResult:
    ok: bool
    problems: list[str]
    latency_ms: dict[str, float]


class Canary:
    def __init__(self, targets: list[CanaryTarget], telemetry: Telemetry, *, max_ms: float = 6000, timeout_s: float = 15):
        self.targets, self.t, self.max_ms, self.timeout_s = targets, telemetry, max_ms, timeout_s

    async def run(self) -> CanaryResult:
        import asyncio
        problems, lat = [], {}
        for tg in self.targets:
            t0 = time.monotonic()
            try:
                res = await asyncio.wait_for(tg.ask(tg.question), self.timeout_s)
            except Exception as exc:
                problems.append(f"{tg.name}: request failed ({type(exc).__name__})")
                continue
            lat[tg.name] = (time.monotonic() - t0) * 1000
            answer = str(res.get("answer", ""))
            if res.get("outcome") != "ok":
                problems.append(f"{tg.name}: outcome {res.get('outcome')}")
            if tg.expect_phrase.lower() not in answer.lower():
                problems.append(f"{tg.name}: expected fact missing from the answer")
            if not res.get("citations"):
                problems.append(f"{tg.name}: no citation")
            if tg.foreign_marker and tg.foreign_marker.lower() in answer.lower():
                problems.append(f"{tg.name}: ISOLATION - another tenant's marker in the answer")
                self.t.violations.inc()
            if lat[tg.name] > self.max_ms:
                problems.append(f"{tg.name}: slow ({lat[tg.name]:.0f} ms)")
        ok = not problems
        self.t.canary.inc(result="pass" if ok else "fail")
        return CanaryResult(ok, problems, lat)
