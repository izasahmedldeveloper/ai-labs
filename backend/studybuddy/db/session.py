"""Tenant-scoped database access (LLD 01 s3, LLD 02 s2): one transaction = one tenant, one user, one role.

`Database.scoped(...)` is the ONLY way application code gets a connection. It opens a transaction, sets
`app.tenant_id`, `app.user_id` and `app.role` with `set_config(..., is_local => true)`, and yields the connection.
Because the settings are transaction-local they vanish at commit/rollback, so a pooled connection can never carry
one request's tenant into the next (tested). The values must come from the VERIFIED token, never from the client.

Row-level security is what actually protects the data; this module only makes it impossible to forget to set the scope.
It does not stop code that already holds a connection from running `SET app.tenant_id` itself: keep SQL out of request
handlers and behind repositories, and keep database credentials out of anything that runs user-supplied queries.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

ROLES = frozenset({"student", "instructor", "tutor", "tenant_admin"})


class ScopeError(ValueError):
    """The tenant/user/role to scope a transaction to is missing or malformed."""


def _uuid(value: str | None, name: str, *, required: bool) -> str:
    if value is None or value == "":
        if required:
            raise ScopeError(f"{name} is required")
        return ""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ScopeError(f"{name} is not a valid id") from None


class Database:
    """Wraps a `psycopg_pool.AsyncConnectionPool` that connects as an application role (sb_app, sb_auth, ...)."""

    def __init__(self, pool, *, statement_timeout_ms: int = 5000):
        self.pool, self.statement_timeout_ms = pool, statement_timeout_ms

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10, **kw) -> "Database":
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open(wait=True, timeout=10)
        return cls(pool, **kw)

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def scoped(self, tenant_id: str, user_id: str | None = None, role: str | None = None) -> AsyncIterator:
        tenant = _uuid(tenant_id, "tenant_id", required=True)
        user = _uuid(user_id, "user_id", required=False)
        if role is not None and role not in ROLES:
            raise ScopeError("unknown role")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true), "
                    "set_config('app.role', %s, true), set_config('statement_timeout', %s, true)",
                    (tenant, user, role or "", str(self.statement_timeout_ms)))
                yield conn
