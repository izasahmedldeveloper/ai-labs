"""Scheduled ticket supervision (LLD 10 s3, s7): re-notify unclaimed tickets, escalate to the instructor and then the
admin, flag SLA breaches once, and close tickets the student never answered. Per tenant, as `sb_worker`; idempotent
(each pass only touches tickets whose state it can advance)."""
from __future__ import annotations

import json

from .policy import EscalationConfig


class SlaMonitor:
    def __init__(self, db, config: EscalationConfig | None = None):
        self.db, self.cfg = db, config or EscalationConfig()

    async def _emit(self, c, kind: str, ticket_id, extra: dict | None = None) -> None:
        await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'escalation.updated', %s::jsonb)",
                        (json.dumps({"kind": kind, "ticket_id": str(ticket_id), **(extra or {})}),))

    async def run(self, tenant_id: str) -> dict:
        counts = {"renotified": 0, "escalated": 0, "breaches": 0, "closed_no_response": 0}
        async with self.db.scoped(tenant_id) as c:
            # 1. Unclaimed tickets that nobody picked up in time.
            for priority, after_s in self.cfg.renotify_after_s.items():
                rows = await (await c.execute(
                    "SELECT id, notify_count, notify_level FROM escalation_ticket WHERE tenant_id = app_tenant() AND priority = %s "
                    "AND assigned_tutor_id IS NULL AND status IN ('open', 'reopened') "
                    "AND coalesce(last_notified_at, created_at) <= now() - make_interval(secs => %s) FOR UPDATE", (priority, after_s))).fetchall()
                for tid, n, level in rows:
                    if n + 1 < self.cfg.renotifications_per_level or level >= 2:
                        await c.execute("UPDATE escalation_ticket SET notify_count = notify_count + 1, last_notified_at = now() "
                                        "WHERE tenant_id = app_tenant() AND id = %s", (tid,))
                        await self._emit(c, "renotify", tid, {"level": level})
                        counts["renotified"] += 1
                    else:
                        await c.execute("UPDATE escalation_ticket SET notify_level = notify_level + 1, notify_count = 0, last_notified_at = now() "
                                        "WHERE tenant_id = app_tenant() AND id = %s", (tid,))
                        await self._emit(c, "escalated", tid, {"level": level + 1})
                        counts["escalated"] += 1
            # 2. SLA deadline passed on a ticket still being worked: tell the admin once.
            rows = await (await c.execute(
                "SELECT id FROM escalation_ticket WHERE tenant_id = app_tenant() AND status IN ('open', 'assigned', 'in_progress', 'reopened') "
                "AND sla_deadline < now() AND breach_alerted_at IS NULL FOR UPDATE")).fetchall()
            for (tid,) in rows:
                await c.execute("UPDATE escalation_ticket SET breach_alerted_at = now() WHERE tenant_id = app_tenant() AND id = %s", (tid,))
                await self._emit(c, "sla_breach", tid)
                counts["breaches"] += 1
            # 3. The tutor asked, the student never answered.
            rows = await (await c.execute(
                "SELECT t.id FROM escalation_ticket t WHERE t.tenant_id = app_tenant() AND t.status = 'in_progress' AND EXISTS ("
                "  SELECT 1 FROM ticket_reply r WHERE r.tenant_id = t.tenant_id AND r.ticket_id = t.id AND r.author_role = 'tutor' "
                "   AND r.created_at <= now() - make_interval(days => %s) "
                "   AND r.created_at = (SELECT max(r2.created_at) FROM ticket_reply r2 WHERE r2.tenant_id = t.tenant_id AND r2.ticket_id = t.id)) "
                "FOR UPDATE", (self.cfg.no_response_close_days,))).fetchall()
            for (tid,) in rows:
                await c.execute("UPDATE escalation_ticket SET status = 'closed_no_response', updated_at = now() WHERE tenant_id = app_tenant() AND id = %s", (tid,))
                await self._emit(c, "closed_no_response", tid)
                counts["closed_no_response"] += 1
        return counts
