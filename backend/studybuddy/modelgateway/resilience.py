"""Circuit breaker, provider rate limiter and priority lanes (LLD 09 B3, B4).

All three take an injectable clock/sleep so tests are instant and deterministic.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from .types import Busy

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class CircuitBreaker:
    """closed -> (N failures inside the window) -> open -> (cool-off) -> half-open -> one probe -> closed | open."""

    def __init__(self, failure_threshold: int = 5, window_s: float = 30.0, cooloff_s: float = 60.0,
                 clock: Clock = time.monotonic):
        self.failure_threshold, self.window_s, self.cooloff_s, self._clock = failure_threshold, window_s, cooloff_s, clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self.cooloff_s else "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probing:
            self._probing = True  # exactly one trial call at a time
            return True
        return False

    def record_success(self) -> None:
        self._probing = False
        self._opened_at = None
        self._failures.clear()

    def record_failure(self) -> None:
        now = self._clock()
        if self._opened_at is not None:  # a failed probe re-opens for a full cool-off
            self._probing = False
            self._opened_at = now
            return
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold:
            self._opened_at = now

    def cancel_probe(self) -> None:
        """The trial call ended without a verdict (cancelled, throttled): allow another probe."""
        self._probing = False


class TokenBucket:
    """Provider rate limit (requests per minute): short bursts are allowed, the average is enforced.

    Callers reserve a token and sleep until it is theirs; if that would take longer than `max_wait_s` the call is
    refused with `Busy` instead of queueing without bound, so the upstream gets a clean 'busy' (B4).
    """

    def __init__(self, rate_per_s: float, burst: int, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep):
        self.rate, self.burst, self._clock, self._sleep = rate_per_s, float(burst), clock, sleep
        self._tokens, self._stamp = float(burst), clock()

    async def acquire(self, max_wait_s: float) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now
        self._tokens -= 1.0  # reserve
        if self._tokens >= 0:
            return
        wait = -self._tokens / self.rate
        if wait > max_wait_s:
            self._tokens += 1.0  # give the reservation back
            raise Busy("provider rate limit reached")
        await self._sleep(wait)


class Lanes:
    """Separate capacity for live chat and bulk work, so an ingestion flood cannot slow students down (B4)."""

    def __init__(self, live: int, bulk: int):
        self._slots = {"live": asyncio.Semaphore(live), "bulk": asyncio.Semaphore(bulk)}

    @asynccontextmanager
    async def slot(self, lane: str, max_wait_s: float) -> AsyncIterator[None]:
        slots = self._slots.get(lane)
        if slots is None:
            raise ValueError(f"unknown lane {lane!r}")
        if slots.locked():
            try:
                await asyncio.wait_for(slots.acquire(), max_wait_s)
            except TimeoutError:
                raise Busy(f"no free {lane} capacity") from None
        else:
            await slots.acquire()
        try:
            yield
        finally:
            slots.release()
