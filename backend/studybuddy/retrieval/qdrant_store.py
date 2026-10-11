"""Qdrant `ChunkStore` (LLD 05 D3, LLD 04 s4, LLD 01 s3).

One collection per embedding model (`chunks_v{N}`), named vectors `dense` (cosine) and `sparse` (BM25 term weights, with
Qdrant's IDF modifier computing inverse document frequency server-side). Point id = uuid5(tenant, course, chunk id), so
a redelivered write replaces instead of duplicating, and two tenants that happen to use the same chunk id can never
overwrite each other.

Visibility is a payload `status` field: chunks are written `pending`, `publish_version` makes the new version `current`
FIRST and only then marks the old one `superseded`. For a moment both versions can be served (the retriever's de-dupe
absorbs identical text); there is never a moment with neither. Qdrant has no multi-operation transaction, so this ordering
is what replaces the "atomic swap" of LLD 04. Every search filters on tenant, course, `status == current` and
`flagged == false`, and results are re-checked client-side (a violating point is dropped and reported loudly).

Tested with qdrant-client's in-process local mode and, when QDRANT_URL is set, against a real server; see
tests/test_qdrant_store.py. Local mode ignores payload indexes, so index creation is only exercised on a real server.
"""
from __future__ import annotations

import math
import uuid
import zlib
from collections import Counter
from typing import Callable, Iterable, Sequence

from .memory_store import bm25_tokens
from .text import STOPWORDS
from .types import Embedder, Hit, MissingFilterError, SearchFilter, StoredChunk

try:  # the core package must import without the optional dependency
    from qdrant_client import AsyncQdrantClient, models as qm
except ImportError:  # pragma: no cover
    AsyncQdrantClient = None  # type: ignore[assignment,misc]
    qm = None  # type: ignore[assignment]

DENSE, SPARSE = "dense", "sparse"
PENDING, CURRENT, SUPERSEDED = "pending", "current", "superseded"
_POINT_NS = uuid.UUID("5f0c4b8e-3d1a-4c55-9a52-7b6f2f0a9c11")  # fixed: ids must be stable across processes and deploys


def point_id(tenant_id: str, course_id: str, chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NS, "\x1f".join((tenant_id, course_id, chunk_id))))


class Bm25SparseEncoder:
    """Text -> sparse vector. Documents get the BM25 term-frequency component; queries get weight 1 per distinct term,
    and Qdrant multiplies by IDF (collection modifier), so the dot product is BM25.

    Terms are hashed to 31-bit ids (crc32), so two different terms can collide (about 1 in 2 billion per pair: accepted).
    `avg_doc_len` is a PLACEHOLDER (words per chunk); set it from your real chunks (~400 tokens target in LLD 04).
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75, avg_doc_len: float = 250.0):
        self.k1, self.b, self.avg_doc_len = k1, b, avg_doc_len

    @staticmethod
    def _index(term: str) -> int:
        return zlib.crc32(term.encode()) & 0x7FFFFFFF

    def document(self, text: str) -> tuple[list[int], list[float]]:
        tf = Counter(t for t in bm25_tokens(text) if t not in STOPWORDS)
        length = sum(tf.values()) or 1
        norm = self.k1 * (1 - self.b + self.b * length / self.avg_doc_len)
        merged: dict[int, float] = {}
        for term, f in tf.items():
            i = self._index(term)
            merged[i] = merged.get(i, 0.0) + f * (self.k1 + 1) / (f + norm)
        return list(merged), list(merged.values())

    def query(self, text: str) -> tuple[list[int], list[float]]:
        indices = sorted({self._index(t) for t in bm25_tokens(text) if t not in STOPWORDS})
        return indices, [1.0] * len(indices)


def _eq(key: str, value) -> "qm.FieldCondition":
    return qm.FieldCondition(key=key, match=qm.MatchValue(value=value))


def _scope(tenant_id: str, course_id: str) -> list:
    return [_eq("tenant_id", tenant_id), _eq("course_id", course_id)]


class QdrantChunkStore:
    def __init__(self, client: "AsyncQdrantClient", collection: str, *, dim: int,
                 encoder: Bm25SparseEncoder | None = None, on_event: Callable[[dict], None] | None = None):
        if qm is None:  # pragma: no cover
            raise RuntimeError("qdrant-client is not installed (pip install qdrant-client)")
        self.client, self.collection, self.dim = client, collection, dim
        self.encoder, self.on_event = encoder or Bm25SparseEncoder(), on_event
        self.violations = 0

    @classmethod
    def from_url(cls, url: str, collection: str, *, dim: int, api_key: str | None = None, timeout_s: int = 10, **kw):
        """`url=":memory:"` gives qdrant-client's in-process local mode (tests and demos; nothing is shared or kept)."""
        client = (AsyncQdrantClient(location=":memory:") if url == ":memory:"
                  else AsyncQdrantClient(url=url, api_key=api_key, timeout=timeout_s))
        return cls(client, collection, dim=dim, **kw)

    # ------------------------------------------------------------------------------------------ collection
    async def ensure_collection(self) -> None:
        """Create the collection and its payload indexes if missing. A dimension mismatch is an error: changing the
        embedding model means a NEW collection (`chunks_v{N+1}`), never reusing this one (LLD 04 s4)."""
        if await self.client.collection_exists(self.collection):
            info = await self.client.get_collection(self.collection)
            size = info.config.params.vectors[DENSE].size  # type: ignore[index]
            if size != self.dim:
                raise ValueError(f"collection {self.collection!r} holds {size}-dimensional vectors, expected {self.dim}")
            return
        await self.client.create_collection(
            self.collection,
            vectors_config={DENSE: qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)})
        for field, schema in (("tenant_id", qm.PayloadSchemaType.KEYWORD), ("course_id", qm.PayloadSchemaType.KEYWORD),
                              ("status", qm.PayloadSchemaType.KEYWORD), ("document_id", qm.PayloadSchemaType.KEYWORD),
                              ("version_id", qm.PayloadSchemaType.KEYWORD), ("ordinal", qm.PayloadSchemaType.INTEGER),
                              ("flagged", qm.PayloadSchemaType.BOOL)):
            await self.client.create_payload_index(self.collection, field, schema)

    async def switch_alias(self, alias: str, target_collection: str) -> None:
        """Point `alias` at a freshly built collection (zero-downtime embedding-model change, LLD 04 s4)."""
        await self.client.update_collection_aliases(change_aliases_operations=[
            qm.CreateAliasOperation(create_alias=qm.CreateAlias(collection_name=target_collection, alias_name=alias))])

    # ------------------------------------------------------------------------------------------ ingestion side
    @staticmethod
    def _payload(c: StoredChunk, status: str) -> dict:
        return {"chunk_id": c.id, "tenant_id": c.tenant_id, "course_id": c.course_id, "document_id": c.document_id,
                "version_id": c.version_id, "ordinal": c.ordinal, "page": c.page, "heading": c.heading,
                "text": c.text, "flagged": c.flagged, "status": status}

    async def upsert(self, chunks: Sequence[StoredChunk], vectors: Sequence[Sequence[float]], *, batch_size: int = 64) -> None:
        """Idempotent write. A chunk that already exists keeps its status, so a redelivered message cannot hide a
        version that is already live; new chunks start `pending` (invisible) until `publish_version`."""
        for start in range(0, len(chunks), batch_size):
            batch = list(zip(chunks[start:start + batch_size], vectors[start:start + batch_size], strict=True))
            ids = [point_id(c.tenant_id, c.course_id, c.id) for c, _ in batch]
            existing = {str(r.id): (r.payload or {}).get("status")
                        for r in await self.client.retrieve(self.collection, ids, with_payload=["status"])}
            points = []
            for (chunk, vector), pid in zip(batch, ids):
                if len(vector) != self.dim:
                    raise ValueError(f"vector for {chunk.id!r} has {len(vector)} dimensions, expected {self.dim}")
                idx, val = self.encoder.document(chunk.text)
                points.append(qm.PointStruct(
                    id=pid, vector={DENSE: list(vector), SPARSE: qm.SparseVector(indices=idx, values=val)},
                    payload=self._payload(chunk, existing.get(pid) or PENDING)))
            await self.client.upsert(self.collection, points=points, wait=True)

    async def publish_version(self, tenant_id: str, course_id: str, document_id: str, version_id: str) -> None:
        """Make `version_id` the one students see. New version first, old one second (never a gap). Refuses to publish
        a version with no chunks, which would otherwise make the document disappear."""
        doc = _scope(tenant_id, course_id) + [_eq("document_id", document_id)]
        new = qm.Filter(must=doc + [_eq("version_id", version_id)])
        if (await self.client.count(self.collection, count_filter=new, exact=True)).count == 0:
            raise LookupError(f"no chunks stored for {document_id!r} version {version_id!r}; nothing published")
        await self.client.set_payload(self.collection, {"status": CURRENT}, points=new, wait=True)
        old = qm.Filter(must=doc + [_eq("status", CURRENT)], must_not=[_eq("version_id", version_id)])
        await self.client.set_payload(self.collection, {"status": SUPERSEDED}, points=old, wait=True)

    async def publish(self, chunks: Sequence[StoredChunk], embedder: Embedder) -> None:
        """Embed, store and publish in one call (same shape as `InMemoryChunkStore.publish`)."""
        vectors = await embedder.embed([c.text for c in chunks])
        await self.upsert(chunks, vectors)
        for tenant, course, doc, version in sorted({(c.tenant_id, c.course_id, c.document_id, c.version_id) for c in chunks}):
            await self.publish_version(tenant, course, doc, version)

    async def remove_document(self, tenant_id: str, course_id: str, document_id: str) -> None:
        """Delete every chunk of the document; retrieval stops at once (LLD 04 s7)."""
        flt = qm.Filter(must=_scope(tenant_id, course_id) + [_eq("document_id", document_id)])
        await self.client.delete(self.collection, points_selector=qm.FilterSelector(filter=flt), wait=True)

    async def delete_version(self, tenant_id: str, course_id: str, document_id: str, version_id: str) -> None:
        flt = qm.Filter(must=_scope(tenant_id, course_id) + [_eq("document_id", document_id), _eq("version_id", version_id)])
        await self.client.delete(self.collection, points_selector=qm.FilterSelector(filter=flt), wait=True)

    async def set_flagged(self, tenant_id: str, course_id: str, chunk_id: str, flagged: bool = True) -> None:
        """Quarantine (or release) one chunk: flagged chunks are never retrieved (LLD 04 injection scan)."""
        await self.client.set_payload(self.collection, {"flagged": flagged},
                                      points=[point_id(tenant_id, course_id, chunk_id)], wait=True)

    async def count(self, tenant_id: str, course_id: str, *, status: str | None = None) -> int:
        must = _scope(tenant_id, course_id) + ([_eq("status", status)] if status else [])
        return (await self.client.count(self.collection, count_filter=qm.Filter(must=must), exact=True)).count

    # ------------------------------------------------------------------------------------------ search side
    @staticmethod
    def _require(flt: object) -> SearchFilter:
        if not isinstance(flt, SearchFilter):
            raise MissingFilterError("a SearchFilter (tenant_id + course_id) is required for every search")
        return flt

    @staticmethod
    def _visible(flt: SearchFilter) -> "qm.Filter":
        return qm.Filter(must=_scope(flt.tenant_id, flt.course_id) + [_eq("status", CURRENT)],
                         must_not=[_eq("flagged", True)])

    @staticmethod
    def _chunk(p: dict) -> StoredChunk:
        return StoredChunk(id=p["chunk_id"], text=p["text"], tenant_id=p["tenant_id"], course_id=p["course_id"],
                           document_id=p["document_id"], version_id=p["version_id"], ordinal=p.get("ordinal") or 0,
                           page=p.get("page"), heading=p.get("heading"), flagged=bool(p.get("flagged")))

    def _checked(self, flt: SearchFilter, payload: dict | None) -> StoredChunk | None:
        """Second, independent check of the filter: a point that should not be here is dropped and reported loudly."""
        p = payload or {}
        ok = (p.get("tenant_id") == flt.tenant_id and p.get("course_id") == flt.course_id
              and p.get("status") == CURRENT and not p.get("flagged") and "chunk_id" in p and "text" in p)
        if not ok:
            self.violations += 1
            if self.on_event is not None:
                self.on_event({"stage": "vector_store", "decision": "filter_violation",
                               "chunk_id": p.get("chunk_id"), "status": p.get("status")})
            return None
        return self._chunk(p)

    async def dense_search(self, flt: SearchFilter, embedding: Sequence[float], limit: int) -> list[Hit]:
        flt = self._require(flt)
        if not any(embedding) or math.isnan(sum(embedding)):
            return []
        res = await self.client.query_points(self.collection, query=list(embedding), using=DENSE,
                                             query_filter=self._visible(flt), limit=limit, with_payload=True)
        hits = [(self._checked(flt, p.payload), p.score) for p in res.points]
        return [Hit(c, float(s)) for c, s in hits if c is not None]

    async def sparse_search(self, flt: SearchFilter, query: str, limit: int) -> list[Hit]:
        flt = self._require(flt)
        idx, val = self.encoder.query(query)
        if not idx:
            return []
        res = await self.client.query_points(self.collection, query=qm.SparseVector(indices=idx, values=val), using=SPARSE,
                                             query_filter=self._visible(flt), limit=limit, with_payload=True)
        hits = [(self._checked(flt, p.payload), p.score) for p in res.points]
        return [Hit(c, float(s)) for c, s in hits if c is not None and s > 0]

    async def neighbours(self, flt: SearchFilter, chunk: StoredChunk) -> list[StoredChunk]:
        flt = self._require(flt)
        visible = self._visible(flt)
        must = list(visible.must) + [_eq("document_id", chunk.document_id), _eq("version_id", chunk.version_id),
                                     qm.FieldCondition(key="ordinal", match=qm.MatchAny(any=[chunk.ordinal - 1, chunk.ordinal + 1]))]
        points, _ = await self.client.scroll(self.collection, scroll_filter=qm.Filter(must=must, must_not=visible.must_not),
                                             limit=2, with_payload=True)
        found = [c for c in (self._checked(flt, p.payload) for p in points) if c is not None]
        return sorted(found, key=lambda c: c.ordinal)

    async def current_version(self, tenant_id: str, course_id: str, document_id: str) -> str | None:
        """The version students currently see for a document, or None. Lets the semantic cache's read-time freshness
        check (`cache.VersionLookup`) run against this store; in production LLD 01's Postgres is the authority."""
        flt = qm.Filter(must=_scope(tenant_id, course_id) + [_eq("document_id", document_id), _eq("status", CURRENT)])
        points, _ = await self.client.scroll(self.collection, scroll_filter=flt, limit=1, with_payload=["version_id"])
        return (points[0].payload or {}).get("version_id") if points else None

    async def health(self) -> bool:
        try:
            await self.client.get_collection(self.collection)
            return True
        except Exception:
            return False
