"""Change-data-capture consumer (LLD 06 s5): `document.changed` events delete the cache entries built from that document.

Events come from the outbox via Kafka (LLD 04), which delivers at least once, so handling is idempotent and a lost or
late event is still covered by the read-time version check. Malformed events are rejected loudly, not guessed at.
"""
from __future__ import annotations

from .semantic_cache import SemanticCache

KINDS = frozenset({"uploaded", "replaced", "deleted", "settings_changed"})


class CdcConsumer:
    def __init__(self, cache: SemanticCache):
        self.cache = cache
        self.processed = 0

    async def handle(self, event: dict) -> int:
        """Returns how many entries were removed. `settings_changed` purges the whole course."""
        try:
            kind, tenant, course = event["kind"], event["tenant_id"], event["course_id"]
        except KeyError as missing:
            raise ValueError(f"malformed change event, missing {missing}") from None
        if kind not in KINDS or not tenant or not course:
            raise ValueError(f"malformed change event: kind={kind!r}")
        if kind == "settings_changed":
            removed = await self.cache.purge_course(tenant, course)
        else:
            document = event.get("document_id")
            if not document:
                raise ValueError("malformed change event, missing 'document_id'")
            removed = await self.cache.invalidate_document(tenant, course, document)
        self.processed += 1
        return removed
