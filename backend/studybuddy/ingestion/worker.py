"""The ingestion worker (LLD 04 s1, s4, s5, s6). One message = one document version.

Every step is safe to repeat, because Kafka-style delivery is at least once:
  chunk ids are deterministic, Postgres chunks are replaced, vectors are upserted by id, and the version only becomes
  visible at the very end (`complete` flips `current_version_id`, then the index publishes it). A crash anywhere leaves
  the old version live; redelivery finishes the job (and repairs the one gap between the two final steps).

Failure policy (D4): permanent problems mark the version `failed` with a plain-language reason and are NOT retried;
temporary or unexpected ones go to a retry topic with a growing delay (never a sleep inside the worker, which would block
the partition); after the last retry the message is parked on the dead-letter topic and an alert callback fires.
No document text is ever logged: events carry ids, sizes and counts.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Callable, Sequence

from ..guardrails.policy import Policy
from ..retrieval.types import StoredChunk
from .broker import DELETED, DLQ, RETRY_DELAYS_S, RETRY_TOPICS, UPLOADED, Broker
from .chunker import ChunkConfig, DraftChunk, chunk_document
from .parsers import parse
from .repo import VersionRecord, VersionRepo, chunk_id
from .scan import AcceptAllScanner, injection_findings, scan_document
from .storage import IndexWriter, ObjectStore
from .types import IngestionError, Limits, REASONS, TemporaryError

Event = Callable[[dict], None]
GENERIC_FAILURE = "We could not process this file after several tries. Please try uploading it again later."


class InMemoryEmbeddingCache:
    """(content hash, model) -> vector. Lets a new version skip chunks that did not change (LLD 04 s6)."""

    def __init__(self):
        self.vectors: dict[tuple[str, str], list[float]] = {}

    def get(self, h, model):
        return self.vectors.get((h, model))

    def put(self, h, model, vector):
        self.vectors[(h, model)] = list(vector)


class IngestionWorker:
    def __init__(self, repo: VersionRepo, objects: ObjectStore, index: IndexWriter, embedder, broker: Broker, *,
                 embed_model: str = "embed-v1", scanner=None, policy: Policy | None = None, chunk_config: ChunkConfig | None = None,
                 limits: Limits | None = None, batch_size: int = 64, embed_attempts: int = 3, embed_backoff_s: Sequence[float] = (0.5, 2.0),
                 sleep=asyncio.sleep, parse_timeout_s: float = 60.0, embedding_cache=None, on_event: Event | None = None,
                 on_dead_letter: Callable[[dict], None] | None = None):
        self.repo, self.objects, self.index, self.embedder, self.broker = repo, objects, index, embedder, broker
        self.embed_model, self.scanner, self.policy = embed_model, scanner or AcceptAllScanner(), policy
        self.chunk_config, self.limits, self.batch_size = chunk_config, limits or Limits(), batch_size
        self.embed_attempts, self.embed_backoff_s, self.sleep = embed_attempts, tuple(embed_backoff_s), sleep
        self.parse_timeout_s, self.cache = parse_timeout_s, embedding_cache
        self.on_event, self.on_dead_letter = on_event or (lambda e: None), on_dead_letter or (lambda e: None)

    # ------------------------------------------------------------------------------------------------ uploaded
    async def handle(self, msg: dict) -> str:
        """Returns done | skipped | failed | retry | dead_letter. Raises only for infrastructure the worker cannot
        report to (e.g. Postgres down): the message is then left unacknowledged and redelivered."""
        try:
            tenant, version = str(uuid.UUID(msg["tenant_id"])), str(uuid.UUID(msg["version_id"]))
        except (KeyError, ValueError, TypeError):
            await self.broker.publish(DLQ, "malformed", {"reason": "malformed message"})
            self.on_dead_letter({"reason": "malformed message"})
            return "dead_letter"
        rec = await self.repo.load(tenant, version)
        if rec is None:
            await self._dead(msg, "version not found")
            return "dead_letter"
        if rec.status == "failed":
            return "skipped"
        if rec.status == "ready":  # redelivery after success; repair the only gap (ready in Postgres, not yet live in the index)
            if rec.is_current:
                await self.index.publish_version(rec.tenant_id, rec.course_id, rec.document_id, rec.version_id)
            return "skipped"
        if rec.document_status == "deleted":
            await self.repo.fail(rec, "The document was deleted before it finished processing.")
            return "skipped"
        attempts = await self.repo.begin(rec)
        try:
            summary = await self._process(rec)
        except IngestionError as exc:
            await self.repo.fail(rec, exc.message)
            self.on_event({"event": "ingest.failed", "version_id": version, "code": exc.code})
            return "failed"
        except Exception as exc:  # TemporaryError and anything unexpected: retry, then dead-letter
            kind = type(exc).__name__
            if attempts <= len(RETRY_TOPICS):
                await self.broker.publish(RETRY_TOPICS[attempts - 1], rec.document_id, msg, delay_s=RETRY_DELAYS_S[attempts - 1])
                self.on_event({"event": "ingest.retry", "version_id": version, "attempt": attempts, "error": kind})
                return "retry"
            await self.repo.fail(rec, GENERIC_FAILURE)
            await self._dead(msg, f"gave up after {attempts} attempts: {kind}")
            return "dead_letter"
        self.on_event({"event": "ingest.done", "version_id": version, **summary})
        return "done"

    async def _dead(self, msg: dict, reason: str) -> None:
        await self.broker.publish(DLQ, str(msg.get("document_id", "unknown")), {**msg, "dead_letter_reason": reason})
        self.on_dead_letter({"reason": reason, "version_id": msg.get("version_id")})

    async def _process(self, rec: VersionRecord) -> dict:
        try:
            data = await self.objects.get(rec.object_key)
        except FileNotFoundError:
            raise IngestionError("corrupt", "The uploaded file could not be found. Please upload it again.") from None
        if not await self.scanner.is_clean(data):
            raise IngestionError("malware", REASONS["malware"])
        try:
            doc = await asyncio.wait_for(asyncio.to_thread(parse, data, rec.filename, self.limits), self.parse_timeout_s)
        except asyncio.TimeoutError:
            raise IngestionError("corrupt", "The file took too long to read and was stopped.") from None
        drafts = chunk_document(doc, self.chunk_config)
        if not drafts:
            raise IngestionError("empty", REASONS["empty"])
        doc_findings = scan_document(doc, self.policy)
        quarantined = {d.ordinal for d in drafts if injection_findings(d.text, self.policy)}
        vectors, reused = await self._embed(rec, drafts)
        await self.repo.replace_chunks(rec, drafts, quarantined, self.embed_model)
        stored = [StoredChunk(id=chunk_id(rec.version_id, d.ordinal), text=d.text, tenant_id=rec.tenant_id, course_id=rec.course_id,
                              document_id=rec.document_id, version_id=rec.version_id, ordinal=d.ordinal, page=d.page,
                              heading=d.heading_path, flagged=d.ordinal in quarantined) for d in drafts]
        await self.index.upsert(stored, vectors)
        flagged = bool(doc_findings or quarantined)
        swapped, _ = await self.repo.complete(rec, flagged=flagged)
        if swapped:
            await self.index.publish_version(rec.tenant_id, rec.course_id, rec.document_id, rec.version_id)
        else:  # deleted meanwhile, or an even newer version is already live: these vectors will never be served
            await self.index.delete_version(rec.tenant_id, rec.course_id, rec.document_id, rec.version_id)
        await self.repo.audit(rec, "document.flagged" if flagged else "document.indexed")
        return {"chunks": len(drafts), "reused_embeddings": reused, "flagged_chunks": len(quarantined), "swapped": swapped,
                "pages": doc.pages}

    async def _embed(self, rec: VersionRecord, drafts: Sequence[DraftChunk]):
        vectors: list[list[float] | None] = [None] * len(drafts)
        todo = []
        for i, d in enumerate(drafts):
            hit = self.cache.get(d.content_hash, self.embed_model) if self.cache else None
            if hit is not None:
                vectors[i] = hit
            else:
                todo.append(i)
        reused = len(drafts) - len(todo)
        done = reused
        for start in range(0, len(todo), self.batch_size):
            idx = todo[start:start + self.batch_size]
            result = await self._embed_batch([drafts[i].embedding_text for i in idx])
            for i, vec in zip(idx, result, strict=True):
                vectors[i] = vec
                if self.cache:
                    self.cache.put(drafts[i].content_hash, self.embed_model, vec)
            done += len(idx)
            await self.repo.progress(rec, int(done * 100 / len(drafts)))  # "embedding 40 of 120"
        return vectors, reused

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        last = None
        for attempt in range(self.embed_attempts):
            try:
                out = await self.embedder.embed(texts)
                if len(out) != len(texts):
                    raise RuntimeError("embedder returned the wrong number of vectors")
                return out
            except Exception as exc:  # rate limit, network: back off a little, then hand the job to the retry topic
                last = exc
                if attempt + 1 < self.embed_attempts:
                    await self.sleep(self.embed_backoff_s[min(attempt, len(self.embed_backoff_s) - 1)])
        raise TemporaryError(f"embedding failed: {type(last).__name__}")

    # ------------------------------------------------------------------------------------------------ deleted
    async def handle_deleted(self, msg: dict) -> str:
        """`document.deleted`: remove the chunks from the index. Retrieval already stops at the status filter; this is cleanup."""
        try:
            tenant, course, doc = str(uuid.UUID(msg["tenant_id"])), str(uuid.UUID(msg["course_id"])), str(uuid.UUID(msg["document_id"]))
        except (KeyError, ValueError, TypeError):
            await self.broker.publish(DLQ, "malformed", {"reason": "malformed delete message"})
            return "dead_letter"
        await self.index.remove_document(tenant, course, doc)
        return "done"


class IngestionConsumer:
    """Polls the main and retry topics, acknowledging a message only after the worker finished it (at least once)."""

    def __init__(self, broker, worker: IngestionWorker):
        self.broker, self.worker = broker, worker
        self.errors = 0

    async def run_once(self) -> int:
        handled = 0
        for topic in (UPLOADED, *RETRY_TOPICS, DELETED):
            while (d := self.broker.poll(topic)) is not None:
                try:
                    await (self.worker.handle_deleted(d.message) if topic == DELETED else self.worker.handle(d.message))
                except Exception:
                    self.errors += 1  # left unacknowledged: redelivered. (A real consumer would pause briefly here.)
                    break
                self.broker.ack(d)
                handled += 1
        return handled
