"""Signing keys of the identity provider (LLD 02 s2, s10).

Keys are cached. A token with an unknown `kid` triggers at most one refresh per `min_refresh_interval_s` (so a flood of
forged kids cannot hammer the IdP). If the IdP is down, cached keys keep working for `stale_if_error_s` (existing
sessions continue; new key rotations fail, LLD 02 s10).
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from .principal import AuthUnavailable


class StaticJwks:
    def __init__(self, keys: Mapping[str, dict[str, Any]] | list[dict[str, Any]]):
        items = keys.values() if isinstance(keys, Mapping) else keys
        self._keys = {k["kid"]: k for k in items}

    async def get_key(self, kid: str) -> dict[str, Any] | None:
        return self._keys.get(kid)


class HttpJwks:
    def __init__(self, url: str, client, *, ttl_s: float = 600.0, min_refresh_interval_s: float = 30.0,
                 stale_if_error_s: float = 24 * 3600.0, clock=time.monotonic):
        self.url, self.client, self.ttl_s = url, client, ttl_s
        self.min_refresh_interval_s, self.stale_if_error_s, self.clock = min_refresh_interval_s, stale_if_error_s, clock
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None

    async def _refresh(self) -> None:
        self._attempted_at = self.clock()
        try:
            resp = await self.client.get(self.url, timeout=3.0)
            resp.raise_for_status()
            keys = {k["kid"]: k for k in resp.json()["keys"] if "kid" in k}
        except Exception:
            return  # keep what we have; the caller decides whether stale keys are still usable
        self._keys, self._fetched_at = keys, self.clock()

    async def get_key(self, kid: str) -> dict[str, Any] | None:
        now = self.clock()
        fresh = self._fetched_at is not None and now - self._fetched_at < self.ttl_s
        throttled = self._attempted_at is not None and now - self._attempted_at < self.min_refresh_interval_s
        if (not fresh and not throttled) or (kid not in self._keys and not throttled):
            await self._refresh()
        key = self._keys.get(kid)
        if key is not None:
            if self._fetched_at is None or self.clock() - self._fetched_at > self.stale_if_error_s:
                raise AuthUnavailable("signing keys are too old and cannot be refreshed")
            return key
        if self._fetched_at is None:
            raise AuthUnavailable("signing keys have never been fetched")
        return None  # keys are loaded and this kid is not among them: an unknown key, i.e. an invalid token
