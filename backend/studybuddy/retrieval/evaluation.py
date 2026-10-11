"""Offline retrieval evaluation (LLD 05 section 9): Recall@5, MRR, Precision@5 and none-found accuracy.

Run this on every change to chunk size, embedding model, k values, thresholds or reranker, and compare against the
stored baseline before shipping. Instructors (or a reviewed generated set) supply the questions and the chunk ids
that answer them; out-of-scope questions are marked `expect_none` and must return `none_found`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from ..pipeline import RequestContext, Retriever


@dataclass(frozen=True)
class EvalItem:
    question: str
    relevant: frozenset[str] = frozenset()  # chunk ids that answer it
    expect_none: bool = False  # out-of-scope: the right outcome is none_found


@dataclass(frozen=True)
class EvalReport:
    n_answerable: int
    n_out_of_scope: int
    recall_at_5: float  # share of answerable questions with a right chunk among the final chunks
    mrr: float  # mean reciprocal rank of the first right chunk, in relevance order
    precision_at_5: float  # share of returned chunks that are right (averaged per question)
    none_found_accuracy: float  # share of out-of-scope questions correctly returning none_found

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


async def evaluate(retriever: Retriever, items: Sequence[EvalItem],
                   make_ctx: Callable[[str], RequestContext]) -> EvalReport:
    """`make_ctx(question)` builds the request context for the tenant/course under test."""
    hits = reciprocal = precision = 0.0
    answerable = out_of_scope = none_correct = 0
    for item in items:
        ctx = make_ctx(item.question)
        result = await retriever.retrieve(ctx)
        if item.expect_none:
            out_of_scope += 1
            none_correct += 1 if result.none_found else 0
            continue
        answerable += 1
        ranked = ctx.retrieval_trace.get("final_ranked") or [c.id for c in result.chunks]
        first = next((i for i, cid in enumerate(ranked, start=1) if cid in item.relevant), None)
        if first is not None:
            hits += 1
            reciprocal += 1.0 / first
        returned = [c.id for c in result.chunks]
        if returned:
            precision += sum(1 for cid in returned if cid in item.relevant) / len(returned)
    return EvalReport(
        n_answerable=answerable, n_out_of_scope=out_of_scope,
        recall_at_5=hits / answerable if answerable else 0.0,
        mrr=reciprocal / answerable if answerable else 0.0,
        precision_at_5=precision / answerable if answerable else 0.0,
        none_found_accuracy=none_correct / out_of_scope if out_of_scope else 1.0,
    )
