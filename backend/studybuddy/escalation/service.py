"""Ticket creation from the pipeline, and everything the student and tutor do with a ticket (LLD 10 s2-s5).

All queries run in `Database.scoped()` as the acting person, so row-level security (migration 004) is the real gate; the
Python checks only give friendlier errors. Texts that reach a ticket (replies, resolutions, notes) pass through the
caller-supplied `sanitize` first, the same PII/secret redaction the chat path uses (LLD 08): the ticket tables hold
redacted text only.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Callable

from psycopg import errors as pg_errors

from ..db.repos import TutorRepo, TicketMessage
from ..pipeline import Identity, RequestContext
from .policy import EscalationConfig, Plan, plan_for

OPEN_STATUSES = ("open", "assigned", "in_progress", "reopened")
OUTCOMES = ("answered", "referred", "out_of_scope")
PRIORITY_ORDER = "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"


class EscalationUnavailable(RuntimeError):
    """A ticket could not be created (no conversation to attach it to, or the store failed)."""


class RateLimited(RuntimeError):
    """Too many tickets from one student and nothing open to attach to."""


class TicketAccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class TicketOutcome:
    ticket_id: str
    deduped: bool = False
    message_id: str | None = None   # set when this call had to store the student's message itself


def _uuid_or_none(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        return None


class PgEscalation:
    """The pipeline's `Escalation` stage (and `after_miss`, the repeated-miss trigger)."""

    def __init__(self, db, config: EscalationConfig | None = None, on_event: Callable[[dict], None] | None = None):
        self.db, self.cfg, self.on_event = db, config or EscalationConfig(), on_event or (lambda e: None)

    # ------------------------------------------------------------------------------------------------ pipeline stage
    async def create(self, ctx: RequestContext, reason: str) -> str:
        if not ctx.conversation_id:
            raise EscalationUnavailable("no conversation to attach the ticket to")
        try:
            out = await self.open(ctx.identity, ctx.course_id, ctx.conversation_id, reason,
                                  message_id=ctx.user_message_id, message_text=ctx.question, trace_id=ctx.trace_id)
        except RateLimited:
            raise EscalationUnavailable("rate limited") from None
        if out.message_id:
            ctx.user_message_id = out.message_id
        return out.ticket_id

    async def after_miss(self, ctx: RequestContext) -> str | None:
        """Called when retrieval found nothing. Returns a ticket id when this is the Nth miss in a row of the conversation."""
        if not ctx.conversation_id:
            return None
        who = ctx.identity
        async with self.db.scoped(who.tenant_id, who.user_id, who.role) as c:
            rows = await (await c.execute(
                "SELECT pipeline_meta->>'outcome' FROM message WHERE tenant_id = app_tenant() AND conversation_id = %s AND role = 'assistant' "
                "ORDER BY created_at DESC, id DESC LIMIT %s", (ctx.conversation_id, self.cfg.miss_lookback_messages))).fetchall()
        misses = 0
        for (outcome,) in rows:  # consecutive misses only: any answered question resets the count
            if outcome == "none_found":
                misses += 1
            else:
                break
        if misses + 1 < self.cfg.miss_threshold:
            return None
        try:
            return await self.create(ctx, "low_confidence")
        except EscalationUnavailable:
            return None

    # ------------------------------------------------------------------------------------------------ core
    async def open(self, identity: Identity, course_id: str, conversation_id: str, reason: str, *, message_id: str | None = None,
                   message_text: str | None = None, trace_id: str | None = None) -> TicketOutcome:
        plan = plan_for(reason, self.cfg)
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as c:
            # One escalation at a time per student and course: concurrent identical requests queue up here and the second sees the first.
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(app_user_id()::text || %s, 0))", (course_id,))
            open_states = list(OPEN_STATUSES)
            same = await (await c.execute(
                "SELECT id FROM escalation_ticket WHERE tenant_id = app_tenant() AND student_id = app_user_id() AND course_id = %s "
                "AND status = ANY(%s) AND created_at > now() - make_interval(hours => %s) "
                "AND (dedup_key = %s OR (%s = 'urgent' AND priority = 'urgent')) ORDER BY created_at DESC LIMIT 1",
                (course_id, open_states, self.cfg.dedup_window_hours, plan.dedup_key, plan.priority))).fetchone()
            if same:
                self.on_event({"event": "ticket.deduped", "ticket_id": str(same[0])})
                return TicketOutcome(str(same[0]), deduped=True)
            recent = (await (await c.execute(
                "SELECT count(*) FROM escalation_ticket WHERE tenant_id = app_tenant() AND student_id = app_user_id() "
                "AND created_at > now() - interval '1 hour'")).fetchone())[0]
            if recent >= self.cfg.rate_limit_per_hour and plan.priority != "urgent":  # a safety ticket is never rate limited
                latest = await (await c.execute(
                    "SELECT id FROM escalation_ticket WHERE tenant_id = app_tenant() AND student_id = app_user_id() AND status = ANY(%s) "
                    "ORDER BY created_at DESC LIMIT 1", (open_states,))).fetchone()
                if latest:
                    return TicketOutcome(str(latest[0]), deduped=True)
                raise RateLimited("too many tickets")
            created_message = None
            if message_id is None:
                if message_text is None:
                    raise EscalationUnavailable("nothing to attach the ticket to")
                created_message = str((await (await c.execute(
                    "INSERT INTO message (tenant_id, conversation_id, role, content_redacted, trace_id) "
                    "VALUES (app_tenant(), %s, 'user', %s, %s) RETURNING id", (conversation_id, message_text, trace_id))).fetchone())[0])
                message_id = created_message
            sla_hours = self.cfg.sla_hours.get(plan.priority, 24)
            ticket_id = str((await (await c.execute(
                "INSERT INTO escalation_ticket (tenant_id, course_id, conversation_id, message_id, reason, priority, student_id, trigger, "
                "restricted, dedup_key, sla_deadline) VALUES (app_tenant(), %s, %s, %s, %s, %s, app_user_id(), %s, %s, %s, "
                "now() + make_interval(hours => %s)) RETURNING id",
                (course_id, conversation_id, message_id, reason[:200], plan.priority, plan.trigger.value, plan.restricted,
                 plan.dedup_key[:200], sla_hours))).fetchone())[0])
            event = {"kind": "created", "ticket_id": ticket_id, "course_id": course_id, "priority": plan.priority,
                     "trigger": plan.trigger.value, "restricted": plan.restricted}
            await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'escalation.created', %s::jsonb)", (json.dumps(event),))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), 'ticket.open', %s)", (f"ticket:{ticket_id}",))
        self.on_event({"event": "ticket.created", "ticket_id": ticket_id, "priority": plan.priority, "trigger": plan.trigger.value})
        return TicketOutcome(ticket_id, False, created_message)


class TicketService:
    """What students, tutors and admins do with tickets. Each method acts as the given identity."""

    def __init__(self, db, escalation: PgEscalation, sanitize: Callable[[str], str] = lambda t: t):
        self.db, self.escalation, self.sanitize = db, escalation, sanitize

    def _scope(self, identity: Identity):
        return self.db.scoped(identity.tenant_id, identity.user_id, identity.role)

    @staticmethod
    def _need_uuid(value: str) -> str:
        v = _uuid_or_none(value)
        if v is None:
            raise TicketAccessDenied("not found")
        return v

    # ------------------------------------------------------------------------------------------------ student
    async def request_tutor(self, identity: Identity, course_id: str, conversation_id: str, note: str = "") -> TicketOutcome:
        """The explicit "Ask a tutor" button. The note is redacted before it is stored."""
        text = self.sanitize(note).strip() or "I'd like to talk to a tutor."
        return await self.escalation.open(identity, course_id, conversation_id, "user_request", message_text=text)

    async def my_tickets(self, identity: Identity) -> list[dict]:
        async with self._scope(identity) as c:
            rows = await (await c.execute(
                "SELECT id, course_id, status, priority, sla_deadline, created_at FROM escalation_ticket "
                "WHERE tenant_id = app_tenant() AND student_id = app_user_id() ORDER BY created_at DESC LIMIT 50")).fetchall()
        return [{"ticket_id": str(r[0]), "course_id": str(r[1]), "status": r[2], "priority": r[3], "expected_by": r[4], "created_at": r[5]} for r in rows]

    async def view(self, identity: Identity, ticket_id: str) -> dict:
        ticket_id = self._need_uuid(ticket_id)
        async with self._scope(identity) as c:
            t = await (await c.execute(
                "SELECT status, priority, sla_deadline, resolution, outcome FROM escalation_ticket WHERE tenant_id = app_tenant() AND id = %s "
                "AND student_id = app_user_id()", (ticket_id,))).fetchone()
            if t is None:
                raise TicketAccessDenied("not found")
            replies = await (await c.execute("SELECT author_role, body, created_at FROM ticket_reply WHERE tenant_id = app_tenant() AND ticket_id = %s "
                                             "ORDER BY created_at, id", (ticket_id,))).fetchall()
        return {"status": t[0], "priority": t[1], "expected_by": t[2], "resolution": t[3], "outcome": t[4],
                "replies": [{"from": "Tutor" if r[0] == "tutor" else "You", "text": r[1], "at": r[2]} for r in replies]}

    async def _student_status(self, identity: Identity, ticket_id: str, new: str, kind: str, allowed_from: tuple[str, ...]) -> bool:
        ticket_id = self._need_uuid(ticket_id)
        async with self._scope(identity) as c:
            try:
                n = (await c.execute("UPDATE escalation_ticket SET status = %s, updated_at = now() WHERE tenant_id = app_tenant() AND id = %s "
                                     "AND student_id = app_user_id() AND status = ANY(%s)", (new, ticket_id, list(allowed_from)))).rowcount
            except pg_errors.InsufficientPrivilege:
                raise TicketAccessDenied("not allowed") from None
            if n != 1:
                return False
            await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'escalation.updated', %s::jsonb)",
                            (json.dumps({"kind": kind, "ticket_id": ticket_id}),))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), %s, %s)",
                            (f"ticket.{kind}", f"ticket:{ticket_id}"))
        return True

    async def reopen(self, identity: Identity, ticket_id: str) -> bool:
        """"Still need help": only a resolved ticket can be reopened."""
        return await self._student_status(identity, ticket_id, "reopened", "reopened", ("resolved",))

    async def cancel(self, identity: Identity, ticket_id: str) -> bool:
        return await self._student_status(identity, ticket_id, "closed", "cancelled", OPEN_STATUSES)

    # ------------------------------------------------------------------------------------------------ both
    async def reply(self, identity: Identity, ticket_id: str, body: str) -> None:
        """A student adds detail, or the assigned tutor answers. Stored redacted."""
        ticket_id = self._need_uuid(ticket_id)
        if identity.role not in ("student", "tutor"):
            raise TicketAccessDenied("not allowed")
        text = self.sanitize(body).strip()
        if not text or len(text) > 4000:
            raise ValueError("a reply must be between 1 and 4000 characters")
        async with self._scope(identity) as c:
            try:
                await c.execute("INSERT INTO ticket_reply (tenant_id, ticket_id, author_id, author_role, body) VALUES (app_tenant(), %s, app_user_id(), %s, %s)",
                                (ticket_id, identity.role, text))
                if identity.role == "tutor":
                    await c.execute("UPDATE escalation_ticket SET first_response_at = coalesce(first_response_at, now()), updated_at = now(), "
                                    "status = CASE WHEN status = 'assigned' THEN 'in_progress' ELSE status END "
                                    "WHERE tenant_id = app_tenant() AND id = %s AND assigned_tutor_id = app_user_id()", (ticket_id,))
            except pg_errors.InsufficientPrivilege:
                raise TicketAccessDenied("not allowed") from None
            kind = "reply" if identity.role == "tutor" else "student_reply"
            await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'escalation.updated', %s::jsonb)",
                            (json.dumps({"kind": kind, "ticket_id": ticket_id}),))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), 'ticket.reply', %s)", (f"ticket:{ticket_id}",))

    # ------------------------------------------------------------------------------------------------ tutor
    async def queue(self, identity: Identity) -> list[dict]:
        """Unclaimed tickets this person may take: metadata only, no student text."""
        async with self._scope(identity) as c:
            rows = await (await c.execute(
                f"SELECT id, course_id, priority, trigger, restricted, created_at, sla_deadline FROM escalation_ticket "
                f"WHERE tenant_id = app_tenant() AND assigned_tutor_id IS NULL AND status IN ('open', 'reopened') "
                f"ORDER BY {PRIORITY_ORDER}, created_at LIMIT 100")).fetchall()
        return [{"ticket_id": str(r[0]), "course_id": str(r[1]), "priority": r[2], "trigger": r[3], "restricted": r[4], "created_at": r[5], "due": r[6]}
                for r in rows]

    async def claim(self, identity: Identity, ticket_id: str) -> bool:
        ticket_id = self._need_uuid(ticket_id)
        async with self._scope(identity) as c:
            try:
                n = (await c.execute(
                    "UPDATE escalation_ticket SET assigned_tutor_id = app_user_id(), status = 'assigned', claimed_at = now(), updated_at = now() "
                    "WHERE tenant_id = app_tenant() AND id = %s AND assigned_tutor_id IS NULL AND status IN ('open', 'reopened')", (ticket_id,))).rowcount
            except pg_errors.InsufficientPrivilege:
                raise TicketAccessDenied("not allowed") from None
            if n != 1:
                return False  # taken by someone else, or not yours to take
            await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'escalation.updated', %s::jsonb)",
                            (json.dumps({"kind": "claimed", "ticket_id": ticket_id}),))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), 'ticket.claim', %s)", (f"ticket:{ticket_id}",))
        return True

    async def read_conversation(self, identity: Identity, ticket_id: str, *, include_raw: bool = False) -> list[TicketMessage]:
        """The bounded, audited window (D4). Raises for anyone but the assigned tutor."""
        try:
            return await TutorRepo(self.db).read_ticket(identity, ticket_id, include_raw=include_raw)
        except pg_errors.InsufficientPrivilege:
            raise TicketAccessDenied("not allowed") from None

    async def resolve(self, identity: Identity, ticket_id: str, outcome: str, resolution: str, *,
                      bot_could_have_answered: bool | None = None, retrieval_gap: bool | None = None) -> None:
        ticket_id = self._need_uuid(ticket_id)
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        text = self.sanitize(resolution).strip()
        async with self._scope(identity) as c:
            try:
                n = (await c.execute(
                    "UPDATE escalation_ticket SET status = 'resolved', outcome = %s, resolution = %s, resolved_at = now(), updated_at = now(), "
                    "bot_could_have_answered = coalesce(%s, bot_could_have_answered), retrieval_gap = coalesce(%s, retrieval_gap) WHERE tenant_id = app_tenant() AND id = %s AND assigned_tutor_id = app_user_id() "
                    "AND status IN ('assigned', 'in_progress', 'reopened')", (outcome, text, bot_could_have_answered, retrieval_gap, ticket_id))).rowcount
            except pg_errors.InsufficientPrivilege:
                raise TicketAccessDenied("not allowed") from None
            if n != 1:
                raise TicketAccessDenied("not allowed")
            await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'escalation.updated', %s::jsonb)",
                            (json.dumps({"kind": "resolved", "ticket_id": ticket_id}),))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), 'ticket.resolve', %s)", (f"ticket:{ticket_id}",))
