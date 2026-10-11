"""The upload API's logic (LLD 04 s1, s6): validate cheaply, store the file, write version + outbox event in ONE
transaction, answer fast. Runs as `sb_app` for the calling instructor, so RLS decides whose course it is."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from ..auth.policy import role_allows
from ..pipeline import Identity
from .parsers import sniff
from .storage import ObjectStore
from .types import IngestionError, Limits, REASONS


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class UploadResult:
    document_id: str
    version_id: str
    version_no: int
    status: str = "processing"


def _safe_name(filename: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])[:100].strip(".") or "upload"
    return name


class DocumentService:
    def __init__(self, db, objects: ObjectStore, limits: Limits | None = None):
        self.db, self.objects, self.limits = db, objects, limits or Limits()

    async def _may_manage(self, conn, course_id: str, identity: Identity) -> bool:
        if identity.role == "tenant_admin":
            return True
        return bool((await (await conn.execute("SELECT is_course_instructor(%s)", (course_id,))).fetchone())[0])

    async def upload(self, identity: Identity, course_id: str, filename: str, data: bytes, document_id: str | None = None) -> UploadResult:
        if not role_allows(identity.role, "document.write") and identity.role != "tenant_admin":
            raise AccessDenied("not allowed")
        if len(data) > self.limits.max_bytes:
            raise IngestionError("too_large", REASONS["too_large"])
        if not data:
            raise IngestionError("empty", REASONS["empty"])
        name = _safe_name(filename)
        sniff(data, name)  # real type from the bytes; rejects mismatches and unsupported types before anything is stored
        version_id = str(uuid.uuid4())
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as c:
            if not await self._may_manage(c, course_id, identity):
                raise AccessDenied("not allowed")
            if document_id is None:
                document_id = str((await (await c.execute(
                    "INSERT INTO document (tenant_id, course_id, title) VALUES (app_tenant(), %s, %s) RETURNING id",
                    (course_id, name))).fetchone())[0])
                version_no = 1
            else:
                row = await (await c.execute("SELECT status FROM document WHERE tenant_id = app_tenant() AND id = %s AND course_id = %s FOR UPDATE",
                                             (document_id, course_id))).fetchone()
                if row is None or row[0] != "active":
                    raise AccessDenied("not allowed")  # unknown, deleted, or someone else's: same answer
                version_no = (await (await c.execute("SELECT coalesce(max(version_no), 0) + 1 FROM document_version "
                                                     "WHERE tenant_id = app_tenant() AND document_id = %s", (document_id,))).fetchone())[0]
            key = f"{identity.tenant_id}/{course_id}/{document_id}/{version_id}/{name}"
            await self.objects.put(key, data)  # inside the transaction: if the commit fails we delete it below
            try:
                await c.execute("INSERT INTO document_version (id, tenant_id, document_id, version_no, object_key, sha256) "
                                "VALUES (%s, app_tenant(), %s, %s, %s, %s)", (version_id, document_id, version_no, key, hashlib.sha256(data).hexdigest()))
                msg = {"tenant_id": identity.tenant_id, "course_id": course_id, "document_id": document_id, "version_id": version_id, "object_key": key}
                await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'document.uploaded', %s::jsonb)", (json.dumps(msg),))
                await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), %s, %s)",
                                ("document.replace" if version_no > 1 else "document.upload", f"document:{document_id}"))
            except Exception:
                await self.objects.delete(key)
                raise
        return UploadResult(document_id, version_id, version_no)

    async def delete(self, identity: Identity, document_id: str) -> None:
        """Soft delete (D6): hidden at once; chunks removed by the worker; hard delete after the grace period."""
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as c:
            row = await (await c.execute("SELECT course_id, status FROM document WHERE tenant_id = app_tenant() AND id = %s FOR UPDATE",
                                         (document_id,))).fetchone()
            if row is None or not await self._may_manage(c, str(row[0]), identity):
                raise AccessDenied("not allowed")
            if row[1] == "deleted":
                return
            course = str(row[0])
            updated = await c.execute("UPDATE document SET status = 'deleted', deleted_at = now() WHERE tenant_id = app_tenant() AND id = %s", (document_id,))
            if updated.rowcount != 1:
                raise AccessDenied("not allowed")
            base = {"tenant_id": identity.tenant_id, "course_id": course, "document_id": document_id}
            for typ, payload in (("document.deleted", base), ("document.changed", {**base, "kind": "deleted"})):
                await c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), %s, %s::jsonb)", (typ, json.dumps(payload)))
            await c.execute("INSERT INTO audit_log (tenant_id, actor_id, action, target) VALUES (app_tenant(), app_user_id(), 'document.delete', %s)",
                            (f"document:{document_id}",))

    async def status(self, identity: Identity, document_id: str) -> list[dict]:
        async with self.db.scoped(identity.tenant_id, identity.user_id, identity.role) as c:
            rows = await (await c.execute("SELECT id, version_no, status, error_reason, progress, flagged_injection FROM document_version "
                                          "WHERE tenant_id = app_tenant() AND document_id = %s ORDER BY version_no", (document_id,))).fetchall()
        return [{"version_id": str(r[0]), "version_no": r[1], "status": r[2], "reason": r[3], "progress": r[4], "flagged": r[5]} for r in rows]
