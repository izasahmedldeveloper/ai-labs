"""Instant revocation for disabled users (LLD 02 s8). Keyed by subject or token id. In-memory stand-in for Redis."""
from __future__ import annotations

import time
from typing import Protocol


class Denylist(Protocol):
    async def is_denied(self, tenant_id: str, subject: str, jti: str | None) -> bool: ...


class InMemoryDenylist:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._until: dict[tuple, float] = {}

    def deny_subject(self, tenant_id: str, subject: str, ttl_s: float = 900.0) -> None:
        self._until[("sub", tenant_id, subject)] = self.clock() + ttl_s

    def deny_token(self, jti: str, ttl_s: float = 900.0) -> None:
        self._until[("jti", jti)] = self.clock() + ttl_s

    async def is_denied(self, tenant_id: str, subject: str, jti: str | None) -> bool:
        now = self.clock()
        keys = [("sub", tenant_id, subject)] + ([("jti", jti)] if jti else [])
        return any(self._until.get(k, 0) > now for k in keys)
