"""Repositories for the request path (LLD 01). Every method opens a scoped transaction for the caller's identity, so
Postgres row-level security decides what is visible; the Python checks here are convenience, not the defence.

Failures raise (the caller decides): the chat API treats an enrollment-check failure as "cannot verify" (503), a history
failure as "no history", and a persistence failure as "log and move on".
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from ..pipeline import Identity, RequestContext
from .session import Database, ScopeError


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class ConversationState:
    conversation_id: str
    history: tuple[str, ...]  # oldest first, starts with a user turn


class EnrollmentRepo:
    def __init__(self, db: Database):
        self.db = db

    async def is_enrolled(self, identity: Identity, course_id: str) -> bool:
        """The `check_enrollment` callable of `api.create_app`. An id that is not a UUID is simply 'not enrolled'."""
        if not _is_uuid(course_id):
            return False
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as conn:
            row = await (await conn.execute(
                "SELECT 1 FROM enrollment WHERE tenant_id = app_tenant() AND course_id = %s AND user_id = app_user_id()",
                (course_id,))).fetchone()
        return row is not None


class ConversationRepo:
    """One rolling conversation per user per course (LLD 01 D1). Explicit conversation ids come with the frontend."""

    def __init__(self, db: Database, history_turns: int = 6):
        self.db, self.history_turns = db, history_turns

    async def load(self, identity: Identity, course_id: str) -> ConversationState:
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as conn:
            row = await (await conn.execute(
                "SELECT id FROM conversation WHERE tenant_id = app_tenant() AND user_id = app_user_id() AND course_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (course_id,))).fetchone()
            if row is None:
                row = await (await conn.execute(
                    "INSERT INTO conversation (tenant_id, user_id, course_id) VALUES (app_tenant(), app_user_id(), %s) RETURNING id",
                    (course_id,))).fetchone()
                return ConversationState(str(row[0]), ())
            conversation_id = row[0]
            rows = await (await conn.execute(
                "SELECT role, content_redacted FROM message WHERE tenant_id = app_tenant() AND conversation_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s", (conversation_id, self.history_turns))).fetchall()
        turns = [(r, t) for r, t in reversed(rows)]
        while turns and turns[0][0] != "user":  # the prompt expects user, assistant, user, ...
            turns.pop(0)
        return ConversationState(str(conversation_id), tuple(t for _, t in turns))

    async def record_turn(self, ctx: RequestContext) -> None:
        """Persist the question and the answer for a finished request. Only REDACTED text is stored (LLD 01 s5):
        `ctx.question` is the sanitised question; `ctx.original_question` is never written."""
        if not ctx.conversation_id:
            return
        who = ctx.identity
        usage = ctx.model_call or {}
        meta = {
            "policy_version": ctx.policy_version, "cache_result": ctx.cache_result, "cache": ctx.cache_trace,
            "router": ctx.router_trace, "route_reason": ctx.route_reason, "retrieval_confidence": ctx.retrieval_confidence,
            "retrieval": {k: v for k, v in ctx.retrieval_trace.items() if isinstance(v, (int, float, bool, str, list))},
            "rewritten_query": ctx.rewritten_query, "outcome": ctx.outcome, "budget_state": ctx.budget_state.value,
            "groundedness": ctx.usage.get("groundedness"), "notices": list(ctx.notices),
        }
        meta_json = json.dumps(meta, default=str)[:20000]
        cited = [c.id for c in ctx.chunks]
        total_ms = int((__import__("time").monotonic() - ctx.started_at) * 1000)
        async with self.db.scoped(who.tenant_id, who.user_id, who.role) as conn:
            await conn.execute(
                "INSERT INTO message (tenant_id, conversation_id, role, content_redacted, trace_id) "
                "VALUES (app_tenant(), %s, 'user', %s, %s)", (ctx.conversation_id, ctx.question, ctx.trace_id))
            if ctx.answer:
                route = ctx.route if ctx.route in ("cache", "rule", "redirect", "cheap_llm", "strong_llm", "human") else None
                await conn.execute(
                    "INSERT INTO message (tenant_id, conversation_id, role, content_redacted, route, model, cited_chunk_ids, "
                    "tokens_in, tokens_out, cost_usd, latency_ms, trace_id, pipeline_meta) "
                    "VALUES (app_tenant(), %s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (ctx.conversation_id, ctx.answer, route, usage.get("model"), cited, usage.get("tokens_in"),
                     usage.get("tokens_out"), usage.get("cost_usd"), total_ms, ctx.trace_id, meta_json))


class AuditRepo:
    def __init__(self, db: Database):
        self.db = db

    async def log(self, identity: Identity, action: str, target: str | None = None, ip: str | None = None) -> None:
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as conn:
            await conn.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target, ip) VALUES (app_tenant(), app_user_id(), %s, %s, %s)",
                               (action, target, ip))


@dataclass(frozen=True)
class TicketMessage:
    message_id: str
    role: str
    content_redacted: str
    raw_content: str | None
    created_at: Any


class TutorRepo:
    """The only way a tutor reads a student's conversation (LLD 01 D4): through the audited database function."""

    def __init__(self, db: Database):
        self.db = db

    async def read_ticket(self, identity: Identity, ticket_id: str, *, include_raw: bool = False) -> list[TicketMessage]:
        if not _is_uuid(ticket_id):
            raise ScopeError("ticket id is not valid")
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as conn:
            rows = await (await conn.execute("SELECT * FROM tutor_read_ticket(%s, %s)", (ticket_id, include_raw))).fetchall()
        return [TicketMessage(str(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
