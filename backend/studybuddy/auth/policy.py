"""One place that answers "may this person do this?" (LLD 02 s5). Pure and deny-by-default.

Allowed = the role allows the action AND the resource is in scope. Scope facts (enrolled, owns the course, assigned
to the ticket) are looked up by the caller (through row-level-security-protected queries) and passed in; this
function never touches I/O, so the whole table is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass

from .principal import Principal

ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "student": frozenset({"chat.ask", "conversation.read_own"}),
    "instructor": frozenset({"chat.ask", "conversation.read_own", "document.write", "analytics.view"}),
    "tutor": frozenset({"chat.ask", "conversation.read_own", "ticket.open"}),
    "tenant_admin": frozenset({"admin.manage", "audit.view", "analytics.view"}),
}

# Scope required on top of the role. Actions not listed here need the role only.
SCOPE_FACT: dict[str, str] = {
    "chat.ask": "enrolled",
    "document.write": "course_instructor",
    "analytics.view": "course_instructor",
    "ticket.open": "ticket_assigned_to_me",
}
# tenant_admin may view analytics for any course of the tenant (the tenant boundary is enforced by RLS).
ADMIN_SCOPE_EXEMPT = frozenset({"analytics.view"})


@dataclass(frozen=True)
class Facts:
    enrolled: bool = False
    course_instructor: bool = False
    ticket_assigned_to_me: bool = False


def role_allows(role: str, action: str) -> bool:
    return action in ROLE_ACTIONS.get(role, frozenset())


def authorize(principal: Principal, action: str, resource_tenant_id: str | None = None, facts: Facts | None = None) -> bool:
    if not role_allows(principal.role, action):
        return False
    if resource_tenant_id is not None and resource_tenant_id != principal.tenant_id:
        return False
    needed = SCOPE_FACT.get(action)
    if needed is None:
        return True
    if principal.role == "tenant_admin" and action in ADMIN_SCOPE_EXEMPT:
        return True
    return bool(getattr(facts or Facts(), needed))
