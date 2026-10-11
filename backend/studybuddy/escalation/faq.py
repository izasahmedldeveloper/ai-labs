"""Tutor-approved answers become FAQ entries and cache entries (LLD 10 s6)."""
from __future__ import annotations

import json
from typing import Callable, Sequence

from ..pipeline import Identity
from .service import TicketAccessDenied, _uuid_or_none


class FaqService:
    def __init__(self, db, cache, sanitize: Callable[[str], str]):
        self.db, self.cache, self.sanitize = db, cache, sanitize

    async def promote(self, identity: Identity, ticket_id: str, question: str, answer: str,
                      source_tags: Sequence[tuple[str, str]] = ()) -> dict:
        """The assigned tutor (or an admin) approves `question` -> `answer` for the whole course.

        Refused if either text contains personal data or a secret (it would be shown to every student). The FAQ row is the
        record; the cache write is best effort and can be redone with `FaqSync`.
        """
        ticket = _uuid_or_none(ticket_id)
        if ticket is None or identity.role not in ("tutor", "tenant_admin"):
            raise TicketAccessDenied("not allowed")
        q, a = question.strip(), answer.strip()
        if not q or not a:
            raise ValueError("question and answer are required")
        if self.sanitize(q) != q or self.sanitize(a) != a:
            raise ValueError("the text contains personal data or secrets; remove them before approving")
        tags = [[str(d), str(v)] for d, v in source_tags]
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as c:
            row = await (await c.execute(
                "SELECT course_id FROM escalation_ticket WHERE tenant_id = app_tenant() AND id = %s AND status IN ('assigned', 'in_progress', 'resolved', 'reopened') "
                "AND (assigned_tutor_id = app_user_id() OR app_role() = 'tenant_admin')", (ticket,))).fetchone()
            if row is None:
                raise TicketAccessDenied("not allowed")
            course = str(row[0])
            faq_id = (await (await c.execute(
                "INSERT INTO faq_entry (tenant_id, course_id, question, answer, ticket_id, approved_by, source_tags) "
                "VALUES (app_tenant(), %s, %s, %s, %s, app_user_id(), %s::jsonb) RETURNING id", (course, q, a, ticket, json.dumps(tags)))).fetchone())[0]
            await c.execute("UPDATE escalation_ticket SET approved_faq = true, updated_at = now() WHERE tenant_id = app_tenant() AND id = %s", (ticket,))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), 'faq.approve', %s)", (f"faq:{faq_id}",))
        cached = True
        try:
            await self.cache.store_faq(identity.tenant_id, course, q, a, [tuple(t) for t in tags])
        except Exception:
            cached = False
        return {"faq_id": str(faq_id), "course_id": course, "cached": cached}


class FaqSync:
    """Rewrites a tenant's FAQ entries into the cache: after a cache loss, or a guardrail policy change (which makes older
    cache entries unusable by design). Runs as `sb_worker`."""

    def __init__(self, db, cache):
        self.db, self.cache = db, cache

    async def reload(self, tenant_id: str) -> int:
        async with self.db.scoped(tenant_id) as c:
            rows = await (await c.execute("SELECT course_id, question, answer, source_tags FROM faq_entry WHERE tenant_id = app_tenant()")).fetchall()
        for course, q, a, tags in rows:
            await self.cache.store_faq(tenant_id, str(course), q, a, [tuple(t) for t in (tags or [])])
        return len(rows)
