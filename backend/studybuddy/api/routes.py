"""HTTP routes for documents and tickets (LLD 04 s1, LLD 10 s5, LLD 12 s5).

Thin on purpose: authentication gives an identity, the service methods act as that identity, and Postgres row-level
security is the real gate. A caller who may not touch a document or ticket gets the same 404 as one that does not exist.
"""
from __future__ import annotations

import inspect
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..escalation.service import EscalationUnavailable, RateLimited, TicketAccessDenied
from ..ingestion.service import AccessDenied
from ..ingestion.types import IngestionError, Limits

NOT_FOUND = "Not found."
ID = r"^[0-9a-fA-F-]{36}$"


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2000)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


class Resolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str
    resolution: str = Field(min_length=1, max_length=4000)
    bot_could_have_answered: bool | None = None
    retrieval_gap: bool | None = None


class Faq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=4000)
    source_tags: list[tuple[str, str]] = Field(default_factory=list, max_length=20)


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return _iso(obj)


def add_routes(app: FastAPI, authenticate: Callable, *, documents=None, tickets=None, faq=None, conversations=None,
               limits: Limits | None = None) -> None:
    limits = limits or Limits()

    async def me(request: Request):
        result = authenticate(request)
        return (await result if inspect.isawaitable(result) else result).identity

    def denied() -> HTTPException:
        return HTTPException(404, NOT_FOUND)

    if documents is not None:
        @app.post("/courses/{course_id}/documents", status_code=202)
        async def upload(course_id: str, request: Request, identity=Depends(me), document_id: str | None = Query(None, pattern=ID)):
            """Raw file bytes as the body; the name comes in `X-Filename`. Answers fast with status `processing`."""
            filename = request.headers.get("x-filename", "")
            if not filename:
                raise HTTPException(422, "X-Filename header is required.")
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limits.max_bytes:
                raise HTTPException(413, "The file is larger than the 50 MB limit.")
            data = bytearray()
            async for part in request.stream():
                data += part
                if len(data) > limits.max_bytes:
                    raise HTTPException(413, "The file is larger than the 50 MB limit.")
            try:
                r = await documents.upload(identity, course_id, filename, bytes(data), document_id=document_id)
            except IngestionError as exc:
                raise HTTPException(422, exc.message) from None
            except AccessDenied:
                raise denied() from None
            return {"document_id": r.document_id, "version_id": r.version_id, "version_no": r.version_no, "status": r.status}

        @app.get("/documents/{document_id}/status")
        async def doc_status(document_id: str, identity=Depends(me)):
            try:
                rows = await documents.status(identity, document_id)
            except Exception:
                raise denied() from None
            if not rows:
                raise denied()
            return {"versions": _clean(rows)}

        @app.delete("/documents/{document_id}", status_code=204)
        async def doc_delete(document_id: str, identity=Depends(me)):
            try:
                await documents.delete(identity, document_id)
            except (AccessDenied, Exception):
                raise denied() from None

    if tickets is not None:
        async def guarded(call):
            try:
                return await call
            except TicketAccessDenied:
                raise denied() from None
            except RateLimited:
                raise HTTPException(429, "You have opened several requests recently. Please wait before opening another.") from None
            except EscalationUnavailable:
                raise HTTPException(503, "We couldn't reach a tutor right now. Please try again.") from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

        @app.post("/courses/{course_id}/tickets", status_code=201)
        async def ask_tutor(course_id: str, body: Note, identity=Depends(me)):
            if conversations is None:
                raise HTTPException(503, "Unavailable.")
            state = await conversations.load(identity, course_id)
            out = await guarded(tickets.request_tutor(identity, course_id, state.conversation_id, body.note))
            return {"ticket_id": out.ticket_id, "already_open": out.deduped}

        @app.get("/tickets")
        async def my_tickets(identity=Depends(me)):
            return {"tickets": _clean(await guarded(tickets.my_tickets(identity)))}

        @app.get("/tickets/{ticket_id}")
        async def view_ticket(ticket_id: str, identity=Depends(me)):
            return _clean(await guarded(tickets.view(identity, ticket_id)))

        @app.post("/tickets/{ticket_id}/replies", status_code=201)
        async def reply(ticket_id: str, body: Body, identity=Depends(me)):
            await guarded(tickets.reply(identity, ticket_id, body.body))
            return {"ok": True}

        @app.post("/tickets/{ticket_id}/reopen")
        async def reopen(ticket_id: str, identity=Depends(me)):
            if not await guarded(tickets.reopen(identity, ticket_id)):
                raise HTTPException(409, "This request cannot be reopened.")
            return {"ok": True}

        @app.post("/tickets/{ticket_id}/cancel")
        async def cancel(ticket_id: str, identity=Depends(me)):
            if not await guarded(tickets.cancel(identity, ticket_id)):
                raise HTTPException(409, "This request cannot be cancelled.")
            return {"ok": True}

        @app.get("/tutor/queue")
        async def queue(identity=Depends(me)):
            if identity.role != "tutor":
                raise denied()
            return {"tickets": _clean(await guarded(tickets.queue(identity)))}

        @app.post("/tutor/tickets/{ticket_id}/claim")
        async def claim(ticket_id: str, identity=Depends(me)):
            if not await guarded(tickets.claim(identity, ticket_id)):
                raise HTTPException(409, "This ticket was already taken.")
            return {"ok": True}

        @app.get("/tutor/tickets/{ticket_id}/conversation")
        async def conversation(ticket_id: str, identity=Depends(me)):
            try:
                msgs = await tickets.read_conversation(identity, ticket_id)
            except Exception:
                raise denied() from None
            return {"messages": [{"role": m.role, "text": m.content_redacted, "at": _iso(m.created_at)} for m in msgs]}

        @app.post("/tutor/tickets/{ticket_id}/resolve")
        async def resolve(ticket_id: str, body: Resolve, identity=Depends(me)):
            await guarded(tickets.resolve(identity, ticket_id, body.outcome, body.resolution,
                                          bot_could_have_answered=body.bot_could_have_answered, retrieval_gap=body.retrieval_gap))
            return {"ok": True}

    if faq is not None:
        @app.post("/tutor/tickets/{ticket_id}/faq", status_code=201)
        async def promote(ticket_id: str, body: Faq, identity=Depends(me)):
            try:
                return await faq.promote(identity, ticket_id, body.question, body.answer, body.source_tags)
            except TicketAccessDenied:
                raise denied() from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
