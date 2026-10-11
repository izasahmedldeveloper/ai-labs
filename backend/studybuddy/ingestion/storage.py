"""Object storage and the vector-index writer contract (LLD 04 s4, s6). In-memory stand-ins for tests and the demo."""
from __future__ import annotations

from typing import Protocol, Sequence

from ..retrieval.memory_store import InMemoryChunkStore
from ..retrieval.types import StoredChunk


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put(self, key, data):
        self.objects[key] = bytes(data)

    async def get(self, key):
        try:
            return self.objects[key]
        except KeyError:
            raise FileNotFoundError(key) from None

    async def delete(self, key):
        self.objects.pop(key, None)


class IndexWriter(Protocol):
    """The ingestion side of the vector store; `QdrantChunkStore` already has exactly these methods."""

    async def upsert(self, chunks: Sequence[StoredChunk], vectors: Sequence[Sequence[float]]) -> None: ...
    async def publish_version(self, tenant_id: str, course_id: str, document_id: str, version_id: str) -> None: ...
    async def delete_version(self, tenant_id: str, course_id: str, document_id: str, version_id: str) -> None: ...
    async def remove_document(self, tenant_id: str, course_id: str, document_id: str) -> None: ...


class MemoryIndex:
    """`IndexWriter` over `InMemoryChunkStore`: upserted chunks are invisible until their version is published."""

    def __init__(self, store: InMemoryChunkStore | None = None):
        self.store = store or InMemoryChunkStore()

    async def upsert(self, chunks, vectors):
        self.store.add(chunks, vectors)

    async def publish_version(self, tenant_id, course_id, document_id, version_id):
        if self.store.count(tenant_id, course_id, document_id, version_id) == 0:
            raise LookupError("no chunks stored for this version; nothing published")
        self.store.set_current(tenant_id, course_id, document_id, version_id)

    async def delete_version(self, tenant_id, course_id, document_id, version_id):
        self.store.delete_version(tenant_id, course_id, document_id, version_id)

    async def remove_document(self, tenant_id, course_id, document_id):
        self.store.remove_document(tenant_id, course_id, document_id)
