"""Stage runner: timeouts and the required/optional failure rule (LLD 03 section 4).

Optional stages fail OPEN (skipped, a fallback value is used).
Required stages fail CLOSED (StageFailed -> the request stops with a friendly error).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .context import RequestContext, StageTiming

T = TypeVar("T")

USER_MESSAGES = {
    "input_guardrails": "We couldn't check your question right now. Please try again.",
    "retriever": "I couldn't look that up right now. Please try again.",
    "model": "Something went wrong while writing the answer. Please try again.",
    "output_guardrails": "We couldn't verify the answer right now. Please try again.",
    "escalation": "I couldn't reach a tutor right now. Please try again.",
}


class StageFailed(Exception):
    def __init__(self, stage: str, outcome: str):
        super().__init__(f"required stage {stage!r} failed ({outcome})")
        self.stage = stage
        self.outcome = outcome
        self.user_message = USER_MESSAGES.get(stage, "Something went wrong. Please try again.")


class Refused(Exception):
    """A stage deliberately stopped the request (guardrail block, budget limit)."""

    def __init__(self, code: str, user_message: str):
        super().__init__(code)
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True)
class PipelineConfig:
    guardrails_timeout_s: float = 0.100
    cache_timeout_s: float = 0.050
    router_timeout_s: float = 0.050
    retriever_timeout_s: float = 0.300
    model_ttft_timeout_s: float = 2.0
    output_check_timeout_s: float = 0.100
    escalation_timeout_s: float = 1.0
    max_streams_per_user: int = 2
    max_streams_per_tenant: int = 50
    idempotency_ttl_s: float = 300.0
    disconnect_grace_s: float = 2.0  # lets a reconnecting browser reattach before the model call is cancelled
    first_chunk_chars: int = 80
    chunk_chars: int = 200


def record(ctx: RequestContext, stage: str, outcome: str, t0: float) -> None:
    ctx.timings.append(StageTiming(stage, outcome, (time.monotonic() - t0) * 1000))


async def run_stage(
    ctx: RequestContext,
    name: str,
    required: bool,
    timeout_s: float,
    call: Callable[[], Awaitable[T]],
    fallback: T | None = None,
) -> T | None:
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(call(), timeout_s)
    except Exception as exc:  # includes TimeoutError; CancelledError is deliberately not caught
        outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
        if required:
            record(ctx, name, "failed", t0)
            raise StageFailed(name, outcome) from exc
        record(ctx, name, "skipped", t0)
        return fallback
    record(ctx, name, "ok", t0)
    return result
