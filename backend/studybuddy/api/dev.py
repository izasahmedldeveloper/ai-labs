"""Local demo app: the real HTTP/SSE layer on top of the fake pipeline stages.

    cd backend
    uvicorn studybuddy.api.dev:app --reload

    curl -N -X POST localhost:8000/chat \
      -H 'content-type: application/json' \
      -H 'x-tenant-id: t1' -H 'x-user-id: u1' -H 'x-user-role: student' \
      -d '{"question":"What is normalization?","course_id":"c1","request_id":"demo-request-1"}'

DEV ONLY: every user counts as enrolled in every course, and the fake stages answer from canned text.
Replace each fake as the real stages (LLD 05-09) and enrollment (LLD 01) are built.
"""
from __future__ import annotations

import os

from ..pipeline import Identity, Orchestrator, PipelineConfig
from ..pipeline import fakes
from .app import create_app
from .auth import GatewayHeaderAuth


async def allow_all_enrollment(identity: Identity, course_id: str) -> bool:
    return True


def build_dev_orchestrator() -> Orchestrator:
    return Orchestrator(
        guardrails=fakes.FakeGuardrails(), cache=fakes.FakeCache(), router=fakes.FakeRouter(),
        retriever=fakes.FakeRetriever(), generator=fakes.FakeGenerator(token_delay=0.05),
        output_guardrails=fakes.FakeOutputGuardrails(), escalation=fakes.FakeEscalation(),
        config=PipelineConfig(),
    )


def build_dev_app():
    return create_app(
        build_dev_orchestrator(),
        authenticate=GatewayHeaderAuth(os.environ.get("STUDYBUDDY_GATEWAY_SECRET") or None),
        check_enrollment=allow_all_enrollment,
    )


app = build_dev_app()
