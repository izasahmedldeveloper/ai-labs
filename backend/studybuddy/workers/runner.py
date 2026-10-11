"""Background loop that moves work along: outbox relay -> broker -> ingestion worker / notifier, plus the periodic SLA and
cleanup passes. Everything it calls is idempotent and safe to run in more than one process; it only schedules.

`tick()` does one round and is what tests call. `run_forever()` loops it. Tenant ids for the per-tenant jobs come from
`tenants()` (configuration or an admin query): no database role may list every tenant, by design.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

ESCALATION_TOPICS = ("escalation.created", "escalation.updated")


class WorkerRunner:
    def __init__(self, *, relay, broker, ingestion_consumer, notifier=None, sla=None, cleanup=None,
                 tenants: Callable[[], Awaitable[list[str]]] | None = None, periodic_every_s: float = 60.0,
                 on_error: Callable[[str, Exception], None] | None = None, clock=time.monotonic):
        self.relay, self.broker, self.ingestion, self.notifier = relay, broker, ingestion_consumer, notifier
        self.sla, self.cleanup, self.tenants = sla, cleanup, tenants
        self.periodic_every_s, self.on_error, self.clock = periodic_every_s, on_error or (lambda where, exc: None), clock
        self._last_periodic: float | None = None

    async def _safe(self, where: str, call) -> int:
        try:
            return await call()
        except Exception as exc:  # one failing job must not stop the others; the work stays queued and is retried next tick
            self.on_error(where, exc)
            return 0

    async def _notify(self) -> int:
        handled = 0
        for topic in ESCALATION_TOPICS:
            while (d := self.broker.poll(topic)) is not None:
                try:
                    await self.notifier.handle(d.message)
                except Exception as exc:
                    self.on_error("notifier", exc)
                    break  # left unacknowledged: retried next tick
                self.broker.ack(d)
                handled += 1
        return handled

    async def _periodic(self) -> int:
        now, n = self.clock(), 0
        if self._last_periodic is not None and now - self._last_periodic < self.periodic_every_s:
            return 0
        self._last_periodic = now
        for tenant in (await self.tenants()) if self.tenants else []:
            for name, job in (("sla", self.sla), ("cleanup", self.cleanup)):
                if job is not None:
                    await self._safe(name, lambda job=job, tenant=tenant: self._count(job.run(tenant)))
                    n += 1
        return n

    @staticmethod
    async def _count(coro) -> int:
        await coro
        return 1

    async def tick(self) -> dict:
        out = {"relayed": await self._safe("relay", self.relay.run_once)}
        out["ingested"] = await self._safe("ingestion", self.ingestion.run_once)
        out["notified"] = await self._safe("notifier", self._notify) if self.notifier else 0
        out["periodic"] = await self._safe("periodic", self._periodic)
        return out

    async def run_forever(self, stop: asyncio.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            busy = await self.tick()
            if not any(busy.values()):
                try:
                    await asyncio.wait_for(stop.wait(), interval_s)
                except asyncio.TimeoutError:
                    pass
