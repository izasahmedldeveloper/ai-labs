"""Outbox relay (LLD 04 s5): publishes rows written in the same transaction as the data they describe, so a saved row
can never lose its message. Runs as `sb_relay` (reads outbox rows of every tenant, marks them published, nothing else).

Delivery is at least once: if the process dies after publishing but before marking, the row is published again.
Consumers must be idempotent (the worker and the CDC consumer are).
"""
from __future__ import annotations

from .broker import DELETED, UPLOADED

TOPIC_FOR = {"document.uploaded": UPLOADED, "document.deleted": DELETED, "document.changed": "document.changed",
             "escalation.created": "escalation.created", "escalation.updated": "escalation.updated"}


class OutboxRelay:
    def __init__(self, db, broker, *, batch_size: int = 100):
        self.db, self.broker, self.batch_size = db, broker, batch_size

    async def run_once(self) -> int:
        async with self.db.pool.connection() as conn:
            async with conn.transaction():
                rows = await (await conn.execute(
                    "SELECT id, tenant_id, type, payload FROM outbox_event WHERE published_at IS NULL "
                    "ORDER BY created_at, id LIMIT %s FOR UPDATE SKIP LOCKED", (self.batch_size,))).fetchall()
                sent = []
                try:
                    for id_, tenant, typ, payload in rows:
                        topic = TOPIC_FOR.get(typ)
                        if topic is None:
                            continue  # unknown type: left unpublished, visible to operators, never guessed at
                        await self.broker.publish(topic, str(payload.get("document_id") or payload.get("ticket_id") or id_),
                                                  {**payload, "event_id": str(id_), "type": typ, "tenant_id": str(tenant)})
                        sent.append(id_)
                finally:
                    if sent:  # rows already published are marked even if a later publish failed
                        await conn.execute("UPDATE outbox_event SET published_at = now() WHERE id = ANY(%s)", (sent,))
        return len(sent)
