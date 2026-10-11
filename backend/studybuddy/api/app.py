"""FastAPI app for the chat orchestrator (LLD 03 section 5, LLD 12 section 4).

POST /chat takes {question, course_id, request_id} and answers with a Server-Sent Events stream.
Everything that can be decided before streaming starts is a normal HTTP error (401, 403, 422, 429, 503);
everything that happens once the pipeline is running is an in-stream `error` event, because by then the
200 status line has already been sent.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable, Union

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from ..pipeline import BudgetState, Identity, Orchestrator, RequestContext
from .auth import Authenticated, GatewayHeaderAuth
from .sse import DEFAULT_KEEPALIVE_S, aclose_quietly, sse_frames

Authenticator = Callable[[Request], Union[Authenticated, Awaitable[Authenticated]]]
EnrollmentChecker = Callable[[Identity, str], Awaitable[bool]]

BUSY_MESSAGE = "You already have questions in progress. Please wait a moment."
NO_ACCESS_MESSAGE = "You don't have access to this course."
UNAVAILABLE_MESSAGE = "We couldn't check your course access right now. Please try again."

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # tell nginx-style proxies not to buffer the stream
}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    course_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    # Client-generated (LLD 12 section 4); a retry after a dropped connection reuses it (idempotency).
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


_ORDER = [BudgetState.OK, BudgetState.WARNING, BudgetState.SOFT_LIMIT, BudgetState.HARD_LIMIT]


def _worse(a: BudgetState, b: BudgetState) -> BudgetState:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b


def create_app(
    orchestrator: Orchestrator,
    *,
    authenticate: Authenticator | None = None,
    check_enrollment: EnrollmentChecker,
    keepalive_s: float = DEFAULT_KEEPALIVE_S,
    enrollment_timeout_s: float = 0.5,
    busy_retry_after_s: int = 3,
    budget_state: Callable[[str, str], BudgetState] | None = None,
) -> FastAPI:
    """Build the app around an orchestrator.

    `check_enrollment` is required on purpose: there is no safe default for "may this user ask about this
    course", and forgetting it would silently allow every course (LLD 01 section 3a, LLD 03 section 4).
    """
    authenticate = authenticate or GatewayHeaderAuth()
    app = FastAPI(title="StudyBuddy API", version="0.1.0")

    async def current_user(request: Request) -> Authenticated:
        result = authenticate(request)
        return await result if inspect.isawaitable(result) else result

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(body: ChatRequest, user: Authenticated = Depends(current_user)):
        # Enrollment is a required stage (LLD 03 section 4): failure to check means no answer, never a guess.
        try:
            enrolled = await asyncio.wait_for(check_enrollment(user.identity, body.course_id), enrollment_timeout_s)
        except Exception:
            raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from None
        if not enrolled:
            raise HTTPException(status_code=403, detail=NO_ACCESS_MESSAGE)

        ctx = RequestContext(
            identity=user.identity,
            course_id=body.course_id,
            question=body.question,
            request_id=body.request_id,
            budget_state=_worse(user.budget_state, budget_state(user.identity.tenant_id, user.identity.user_id))
            if budget_state else user.budget_state,
        )
        events = orchestrator.handle(ctx).__aiter__()
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            first = None
        except BaseException:
            await aclose_quietly(events)
            raise

        # The orchestrator refuses a user who already has too many active answers (backpressure, LLD 03 section 7).
        # Surface that as a real 429 with Retry-After, which is what the client's rate-limit UX expects (LLD 12 section 5).
        if first is None or (first.type == "error" and first.data.get("code") == "busy"):
            await aclose_quietly(events)
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(busy_retry_after_s)},
                content={"error": {"code": "busy", "message": BUSY_MESSAGE, "retryable": True}},
            )

        return StreamingResponse(
            sse_frames(first, events, keepalive_s),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
            # Belt and braces: if the response never starts (client vanished early) still release the stream.
            background=BackgroundTask(aclose_quietly, events),
        )

    return app
