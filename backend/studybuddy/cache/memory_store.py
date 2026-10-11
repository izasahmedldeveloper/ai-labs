"""In-memory `CacheStore` standing in for the Redis vector index. Exact cosine scan: fine for tests and a demo."""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from typing import Sequence

from .types import CacheEntry, Namespace


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryCacheStore:
    def __init__(self, *, delay_s: float = 0.0, fail: bool = False):
        self.delay_s, self.fail = delay_s, fail
        self._entries: dict[Namespace, dict[str, CacheEntry]] = defaultdict(dict)

    async def _gate(self) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise ConnectionError("cache store down")

    def __len__(self) -> int:
        return sum(len(v) for v in self._entries.values())

    def entries(self, ns: Namespace) -> list[CacheEntry]:
        return list(self._entries.get(ns, {}).values())

    async def search(self, ns: Namespace, embedding: Sequence[float], k: int) -> list[tuple[CacheEntry, float]]:
        await self._gate()
        scored = [(e, cosine(embedding, e.embedding)) for e in self._entries.get(ns, {}).values()]
        scored.sort(key=lambda pair: -pair[1])
        return scored[:k]

    async def put(self, entry: CacheEntry) -> None:
        await self._gate()
        self._entries[entry.namespace][entry.id] = entry

    async def delete(self, ns: Namespace, entry_id: str) -> bool:
        await self._gate()
        return self._entries.get(ns, {}).pop(entry_id, None) is not None

    async def touch(self, ns: Namespace, entry_id: str) -> None:
        entry = self._entries.get(ns, {}).get(entry_id)
        if entry is not None:
            entry.hit_count += 1

    async def delete_document(self, tenant_id: str, course_id: str, document_id: str) -> int:
        await self._gate()
        removed = 0
        for ns, bucket in self._entries.items():
            if ns[0] != tenant_id or ns[1] != course_id:
                continue
            for entry_id in [i for i, e in bucket.items() if any(doc == document_id for doc, _ in e.tags)]:
                del bucket[entry_id]
                removed += 1
        return removed

    async def purge(self, tenant_id: str, course_id: str) -> int:
        await self._gate()
        removed = 0
        for ns in [n for n in self._entries if n[0] == tenant_id and n[1] == course_id]:
            removed += len(self._entries.pop(ns))
        return removed
