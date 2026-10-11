"""Postgres side of ingestion, as `sb_worker` (tenant-scoped) and `sb_app` (the instructor's own request).

`VersionRepo` is the worker's view; `PgVersionRepo` implements it. Everything runs inside `Database.scoped()`, so RLS
applies. Chunk ids are deterministic (uuid5 of version + ordinal): a redelivered job writes the same ids to Postgres and
Qdrant, which is what makes at-least-once delivery safe (LLD 04 s4).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from .chunker import DraftChunk

_NS = uuid.UUID("5b1f0c1e-7f6e-4d51-9a54-0a1f0d6b7e11")


def chunk_id(version_id: str, ordinal: int) -> str:
    return str(uuid.uuid5(_NS, f"{version_id}:{ordinal}"))


@dataclass(frozen=True)
class VersionRecord:
    tenant_id: str
    course_id: str
    document_id: str
    version_id: str
    version_no: int
    object_key: str
    status: str
    attempts: int
    document_status: str          # active | deleted
    is_current: bool

    @property
    def filename(self) -> str:
        return self.object_key.rsplit("/", 1)[-1]


class VersionRepo(Protocol):
    async def load(self, tenant_id: str, version_id: str) -> VersionRecord | None: ...
    async def begin(self, rec: VersionRecord) -> int: ...
    async def progress(self, rec: VersionRecord, percent: int) -> None: ...
    async def fail(self, rec: VersionRecord, reason: str) -> None: ...
    async def replace_chunks(self, rec: VersionRecord, drafts: Sequence[DraftChunk], flagged: set[int], model: str) -> None: ...
    async def complete(self, rec: VersionRecord, *, flagged: bool) -> tuple[bool, str | None]: ...
    async def audit(self, rec: VersionRecord, action: str) -> None: ...


class PgVersionRepo:
    def __init__(self, db):
        self.db = db  # a Database connected as sb_worker

    async def load(self, tenant_id, version_id):
        async with self.db.scoped(tenant_id) as c:
            row = await (await c.execute(
                "SELECT d.course_id, d.id, v.id, v.version_no, v.object_key, v.status, v.attempts, d.status, "
                "d.current_version_id = v.id FROM document_version v JOIN document d ON d.tenant_id = v.tenant_id AND d.id = v.document_id "
                "WHERE v.tenant_id = app_tenant() AND v.id = %s", (version_id,))).fetchone()
        if row is None:
            return None
        return VersionRecord(tenant_id, str(row[0]), str(row[1]), str(row[2]), row[3], row[4], row[5], row[6], row[7], bool(row[8]))

    async def begin(self, rec):
        async with self.db.scoped(rec.tenant_id) as c:
            row = await (await c.execute(
                "UPDATE document_version SET status = 'processing', attempts = attempts + 1, progress = 0, error_reason = NULL "
                "WHERE tenant_id = app_tenant() AND id = %s AND status IN ('uploaded', 'processing') RETURNING attempts",
                (rec.version_id,))).fetchone()
        return row[0] if row else rec.attempts

    async def progress(self, rec, percent):
        async with self.db.scoped(rec.tenant_id) as c:
            await c.execute("UPDATE document_version SET progress = %s WHERE tenant_id = app_tenant() AND id = %s AND status = 'processing'",
                            (max(0, min(99, percent)), rec.version_id))

    async def fail(self, rec, reason):
        async with self.db.scoped(rec.tenant_id) as c:
            await c.execute("UPDATE document_version SET status = 'failed', error_reason = %s WHERE tenant_id = app_tenant() AND id = %s "
                            "AND status <> 'ready'", (reason[:500], rec.version_id))

    async def replace_chunks(self, rec, drafts, flagged, model):
        async with self.db.scoped(rec.tenant_id) as c:
            await c.execute("DELETE FROM chunk WHERE tenant_id = app_tenant() AND version_id = %s", (rec.version_id,))
            for d in drafts:
                await c.execute(
                    "INSERT INTO chunk (id, tenant_id, course_id, document_id, version_id, ordinal, text, page, heading_path, "
                    "content_hash, token_count, embedding_model, flagged) VALUES (%s, app_tenant(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (chunk_id(rec.version_id, d.ordinal), rec.course_id, rec.document_id, rec.version_id, d.ordinal, d.text, d.page,
                     d.heading_path, d.content_hash, d.token_count, model, d.ordinal in flagged))

    async def complete(self, rec, *, flagged):
        """One transaction: version ready, document points at it (only if it is newer than the current one and the
        document is still active), previous version retired, change event written. Returns (swapped, previous_version)."""
        async with self.db.scoped(rec.tenant_id) as c:
            await c.execute("UPDATE document_version SET status = 'ready', progress = 100, flagged_injection = %s "
                            "WHERE tenant_id = app_tenant() AND id = %s", (flagged, rec.version_id))
            doc = await (await c.execute(
                "SELECT d.status, d.current_version_id, cv.version_no FROM document d LEFT JOIN document_version cv "
                "ON cv.tenant_id = d.tenant_id AND cv.id = d.current_version_id WHERE d.tenant_id = app_tenant() AND d.id = %s FOR UPDATE OF d",
                (rec.document_id,))).fetchone()
            status, prev, prev_no = doc
            if status != "active" or (prev is not None and str(prev) != rec.version_id and prev_no >= rec.version_no):
                return False, None
            await c.execute("UPDATE document SET current_version_id = %s WHERE tenant_id = app_tenant() AND id = %s", (rec.version_id, rec.document_id))
            if prev is not None and str(prev) != rec.version_id:
                await c.execute("UPDATE document_version SET retired_at = now() WHERE tenant_id = app_tenant() AND id = %s", (prev,))
            payload = {"kind": "replaced" if prev else "uploaded", "tenant_id": rec.tenant_id, "course_id": rec.course_id,
                       "document_id": rec.document_id, "version_id": rec.version_id}
            await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'document.changed', %s::jsonb)",
                            (json.dumps(payload),))
            return True, (str(prev) if prev else None)

    async def audit(self, rec, action):
        async with self.db.scoped(rec.tenant_id) as c:
            await c.execute("INSERT INTO audit_log (tenant_id, action, target) VALUES (app_tenant(), %s, %s)", (action, f"version:{rec.version_id}"))
