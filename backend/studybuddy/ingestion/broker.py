"""Message transport (LLD 04 s5). The worker depends on this small contract, not on Kafka.

`InMemoryBroker` gives tests the properties that matter: per-topic FIFO, a delivery delay (for retry topics), at-least-once
(a message stays until `ack`), and a key. Nothing here talks to a real Kafka; an adapter would implement `Broker`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

UPLOADED = "document.uploaded"
DELETED = "document.deleted"
RETRY_TOPICS = ("document.uploaded.retry.1", "document.uploaded.retry.2", "document.uploaded.retry.3")
RETRY_DELAYS_S = (10.0, 60.0, 600.0)  # D4
DLQ = "document.uploaded.dlq"


class Broker(Protocol):
    async def publish(self, topic: str, key: str, message: dict, *, delay_s: float = 0.0) -> None: ...


@dataclass
class Delivery:
    topic: str
    key: str
    message: dict
    available_at: float


class InMemoryBroker:
    def __init__(self, clock=time.monotonic):
        self.clock, self.topics, self.published = clock, {}, []

    async def publish(self, topic, key, message, *, delay_s=0.0):
        d = Delivery(topic, key, dict(message), self.clock() + delay_s)
        self.topics.setdefault(topic, []).append(d)
        self.published.append(d)

    def poll(self, topic: str) -> Delivery | None:
        """Next due message, left in the topic until `ack` (at-least-once)."""
        now = self.clock()
        for d in self.topics.get(topic, []):
            if d.available_at <= now:
                return d
        return None

    def ack(self, d: Delivery) -> None:
        self.topics[d.topic].remove(d)

    def pending(self, topic: str) -> int:
        return len(self.topics.get(topic, []))
