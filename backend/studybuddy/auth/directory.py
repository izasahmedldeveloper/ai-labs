"""Maps a verified token to an `app_user` row, creating it on first login (JIT provisioning, LLD 02 s4).

The database is the authority for role and status. The token's roles are used ONLY to pick the starting role of a
brand-new user, mapped to the least privilege that fits, and never to `tenant_admin`: an administrator is made by
another administrator, not by an IdP group. Runs as the narrow `sb_auth` role (it can read/insert app_user of one
tenant and nothing else).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from .principal import AuthError, AuthUnavailable

JIT_ROLE_ORDER = ("student", "tutor", "instructor")  # least to most privileged; tenant_admin is never JIT


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    role: str
    status: str


class UserDirectory(Protocol):
    async def resolve(self, tenant_id: str, subject: str, token_roles: Sequence[str]) -> UserRecord: ...


def initial_role(token_roles: Sequence[str], mapping: dict[str, str] | None = None) -> str:
    mapped = {(mapping or {}).get(r, r) for r in token_roles if isinstance(r, str)}
    best = "student"
    for role in JIT_ROLE_ORDER:
        if role in mapped:
            best = role
    return best


class PgUserDirectory:
    def __init__(self, db, role_mapping: dict[str, str] | None = None):
        self.db, self.role_mapping = db, role_mapping

    async def resolve(self, tenant_id, subject, token_roles):
        try:
            async with self.db.scoped(tenant_id) as conn:
                sel = "SELECT id, role, status FROM app_user WHERE tenant_id = app_tenant() AND idp_subject = %s"
                row = await (await conn.execute(sel, (subject,))).fetchone()
                if row is None:
                    await conn.execute(
                        "INSERT INTO app_user (tenant_id, idp_subject, role) VALUES (app_tenant(), %s, %s) "
                        "ON CONFLICT (tenant_id, idp_subject) DO NOTHING", (subject, initial_role(token_roles, self.role_mapping)))
                    row = await (await conn.execute(sel, (subject,))).fetchone()
        except Exception as exc:
            if "ForeignKeyViolation" in type(exc).__name__:
                raise AuthError("unknown tenant") from None  # a valid token for a tenant we do not have
            raise AuthUnavailable("user store unavailable") from None
        if row is None:
            raise AuthUnavailable("user could not be provisioned")
        return UserRecord(str(row[0]), row[1], row[2])


class InMemoryUserDirectory:
    """For tests and the dev server."""

    def __init__(self, tenants: set[str], role_mapping: dict[str, str] | None = None):
        self.tenants, self.role_mapping, self.users = set(tenants), role_mapping, {}

    async def resolve(self, tenant_id, subject, token_roles):
        if tenant_id not in self.tenants:
            raise AuthError("unknown tenant")
        key = (tenant_id, subject)
        if key not in self.users:
            self.users[key] = UserRecord(str(uuid.uuid4()), initial_role(token_roles, self.role_mapping), "active")
        return self.users[key]
