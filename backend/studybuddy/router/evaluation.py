"""Router evaluation (LLD 07 s9): per-class precision and recall, sensitive recall against the 98% target."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from ..pipeline import Identity, RequestContext

SENSITIVE_RECALL_TARGET = 0.98  # LLD 07 D5


@dataclass(frozen=True)
class RouterExample:
    question: str
    expected_route: str  # a Route value


@dataclass
class RouterReport:
    n: int = 0
    confusion: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))  # (expected, got)
    misses: list[tuple[str, str, str]] = field(default_factory=list)  # (question, expected, got)

    def precision(self, route: str) -> float | None:
        got = sum(c for (e, g), c in self.confusion.items() if g == route)
        return self.confusion[(route, route)] / got if got else None

    def recall(self, route: str) -> float | None:
        expected = sum(c for (e, g), c in self.confusion.items() if e == route)
        return self.confusion[(route, route)] / expected if expected else None

    @property
    def human_recall(self) -> float | None:
        return self.recall("human")

    @property
    def meets_sensitive_target(self) -> bool:
        r = self.human_recall
        return r is not None and r >= SENSITIVE_RECALL_TARGET


async def evaluate_async(router, examples: Iterable[RouterExample]) -> RouterReport:
    report = RouterReport()
    for i, ex in enumerate(examples):
        ctx = RequestContext(identity=Identity("eval", "eval", "student"), course_id="eval", question=ex.question,
                             request_id=f"eval-{i:06d}")
        got = (await router.route(ctx)).route.value
        report.n += 1
        report.confusion[(ex.expected_route, got)] += 1
        if got != ex.expected_route:
            report.misses.append((ex.question, ex.expected_route, got))
    return report


def evaluate(router, examples: Iterable[RouterExample]) -> RouterReport:
    return asyncio.run(evaluate_async(router, examples))
