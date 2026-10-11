"""Turns ticket events into notifications (LLD 10 s2, s3, s7). Runs as `sb_worker`.

Notifications carry ids, priority and trigger only: never student text. Who is told depends on the event:
  created / renotify  -> tutors on the course roster (designated tutors only for restricted tickets); with no tutor
                         configured, the instructor, then the tenant admins. Urgent tickets also alert the on-call list.
  escalated           -> level 1: instructors of the course; level 2: tenant admins.
  sla_breach          -> tenant admins.
  reply / resolved / closed_no_response -> the student.   student_reply / reopened -> the assigned tutor.
Delivery is at least once (events are redelivered), so each event id is handled once per process; channels should also
be idempotent. If every channel fails for a recipient group the event is NOT acknowledged and is retried.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Protocol

from .policy import EscalationConfig


class NotificationChannel(Protocol):
    async def send(self, tenant_id: str, user_id: str, notification: dict) -> None: ...


class InMemoryChannel:
    """Records notifications; stands in for email / in-app / chat webhook (D3), none of which is built."""

    def __init__(self, name: str = "memory", fail: bool = False):
        self.name, self.fail, self.sent = name, fail, []

    async def send(self, tenant_id, user_id, notification):
        if self.fail:
            raise ConnectionError(f"{self.name} down")
        self.sent.append((tenant_id, user_id, notification))


class NotificationFailed(RuntimeError):
    pass


TITLES = {
    "created": "New ticket", "renotify": "Ticket still unclaimed", "escalated": "Unclaimed ticket escalated", "sla_breach": "Ticket past its deadline",
    "reply": "Your tutor replied", "resolved": "Your ticket was resolved", "closed_no_response": "Your ticket was closed",
    "student_reply": "The student replied", "reopened": "A ticket was reopened", "claimed": "Your ticket has a tutor", "cancelled": "A ticket was cancelled",
}


class Notifier:
    def __init__(self, db, channels: list[NotificationChannel], config: EscalationConfig | None = None, *, remember: int = 5000):
        self.db, self.channels, self.cfg = db, channels, config or EscalationConfig()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._remember = remember

    async def _users(self, c, sql: str, params: tuple = ()) -> list[str]:
        return [str(r[0]) for r in await (await c.execute(sql, params)).fetchall()]

    async def _staff(self, c, course: str, restricted: bool) -> list[str]:
        base = ("SELECT e.user_id FROM enrollment e JOIN app_user u ON u.tenant_id = e.tenant_id AND u.id = e.user_id "
                "WHERE e.tenant_id = app_tenant() AND e.course_id = %s AND u.status = 'active' AND e.role_in_course = %s")
        tutors = await self._users(c, base + " AND (NOT %s OR e.sensitive_ok)", (course, "tutor", restricted))
        return tutors or await self._users(c, base, (course, "instructor")) or await self._admins(c)

    async def _admins(self, c) -> list[str]:
        return await self._users(c, "SELECT id FROM app_user WHERE tenant_id = app_tenant() AND role = 'tenant_admin' AND status = 'active'")

    async def recipients(self, tenant_id: str, event: dict) -> list[str]:
        kind, ticket_id = event.get("kind"), event.get("ticket_id")
        async with self.db.scoped(tenant_id) as c:
            t = await (await c.execute("SELECT course_id, student_id, assigned_tutor_id, restricted, priority, trigger, notify_level FROM escalation_ticket "
                                       "WHERE tenant_id = app_tenant() AND id = %s", (ticket_id,))).fetchone()
            if t is None:
                return []
            course, student, tutor, restricted, priority, trigger, level = (str(t[0]), str(t[1]) if t[1] else None, str(t[2]) if t[2] else None, *t[3:])
            event.update(priority=priority, trigger=trigger, course_id=course)
            if kind in ("created", "renotify"):
                if level >= 2:
                    people = await self._admins(c)
                elif level == 1:
                    people = await self._users(c, "SELECT user_id FROM enrollment WHERE tenant_id = app_tenant() AND course_id = %s AND role_in_course = 'instructor'", (course,)) or await self._admins(c)
                else:
                    people = await self._staff(c, course, restricted)
                if priority == "urgent":
                    people = [*people, *self.cfg.oncall_user_ids]
            elif kind == "escalated":
                people = (await self._users(c, "SELECT user_id FROM enrollment WHERE tenant_id = app_tenant() AND course_id = %s AND role_in_course = 'instructor'", (course,))
                          if level == 1 else await self._admins(c)) or await self._admins(c)
                if priority == "urgent":
                    people = [*people, *self.cfg.oncall_user_ids]
            elif kind == "sla_breach":
                people = await self._admins(c)
            elif kind in ("reply", "resolved", "closed_no_response", "claimed"):
                people = [student] if student else []
            elif kind in ("student_reply", "reopened"):
                people = [tutor] if tutor else await self._staff(c, course, restricted)
            else:
                people = []
        return list(dict.fromkeys(people))

    async def handle(self, message: dict) -> dict:
        event_id, tenant = message.get("event_id"), message.get("tenant_id")
        if event_id in self._seen:
            return {"recipients": 0, "duplicate": True}
        event = {k: message[k] for k in ("kind", "ticket_id") if k in message}
        people = await self.recipients(tenant, event)
        note = {"kind": event.get("kind"), "title": TITLES.get(event.get("kind"), "Ticket update"), "ticket_id": event.get("ticket_id"),
                "course_id": event.get("course_id"), "priority": event.get("priority"), "trigger": event.get("trigger")}
        failed = 0
        for user in people:
            delivered = False
            for channel in self.channels:  # first channel that works; the in-app queue is the source of truth anyway
                try:
                    await channel.send(tenant, user, note)
                    delivered = True
                    break
                except Exception:
                    continue
            failed += 0 if delivered else 1
        if people and failed == len(people):
            raise NotificationFailed("every channel failed")  # not remembered: the event will be retried
        self._seen[event_id] = None
        while len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        return {"recipients": len(people), "failed": failed}
