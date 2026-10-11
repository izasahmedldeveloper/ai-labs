"""Stream buffering (reconnect/idempotency) and concurrency limits (LLD 03 section 7)."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator, Callable

from .context import Identity
from .types import Event


class StreamBuffer:
    """Holds every event of one request so a retry with the same request_id can replay or reattach.

    If the last subscriber leaves before the request finishes, `on_abandon` fires after a grace period
    (used to cancel the model call so we stop paying for tokens nobody reads).
    """

    def __init__(self, on_abandon: Callable[[], None] | None = None):
        self.events: list[Event] = []
        self.closed = False
        self.cancelled = False
        self.subscribers = 0
        self.on_abandon = on_abandon
        self._waiters: list[asyncio.Future] = []
        self._abandon_timer: asyncio.TimerHandle | None = None

    def append(self, event: Event) -> None:
        self.events.append(event)
        self._wake()

    def close(self) -> None:
        self.closed = True
        self._wake()

    def _wake(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    async def subscribe(self, grace_s: float) -> AsyncIterator[Event]:
        self.subscribers += 1
        if self._abandon_timer is not None:
            self._abandon_timer.cancel()
            self._abandon_timer = None
        index = 0
        try:
            while True:
                while index < len(self.events):
                    yield self.events[index]
                    index += 1
                if self.closed:
                    return
                waiter = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                await waiter
        finally:
            self.subscribers -= 1
            if self.subscribers == 0 and not self.closed and self.on_abandon is not None:
                self._abandon_timer = asyncio.get_running_loop().call_later(grace_s, self.on_abandon)


class ConcurrencyLimiter:
    """Backpressure: at most N active answers per user and per tenant (the gateway handles requests/minute)."""

    def __init__(self, per_user: int, per_tenant: int):
        self.per_user = per_user
        self.per_tenant = per_tenant
        self._user: dict[tuple[str, str], int] = defaultdict(int)
        self._tenant: dict[str, int] = defaultdict(int)

    def try_acquire(self, who: Identity) -> bool:
        ukey = (who.tenant_id, who.user_id)
        if self._user[ukey] >= self.per_user or self._tenant[who.tenant_id] >= self.per_tenant:
            return False
        self._user[ukey] += 1
        self._tenant[who.tenant_id] += 1
        return True

    def release(self, who: Identity) -> None:
        ukey = (who.tenant_id, who.user_id)
        self._user[ukey] = max(0, self._user[ukey] - 1)
        self._tenant[who.tenant_id] = max(0, self._tenant[who.tenant_id] - 1)
