"""Scheduled cleanup (LLD 04 s6, D5/D6): old-version chunks after 7 days, hard delete after 30 days.

Per tenant, as `sb_worker` (the job is given the tenant ids; there is deliberately no role that lists all tenants).
Idempotent: running it twice does nothing the second time.
"""
from __future__ import annotations


class CleanupJob:
    def __init__(self, db, index, objects, *, old_version_days: float = 7, deleted_grace_days: float = 30):
        self.db, self.index, self.objects = db, index, objects
        self.old_days, self.grace_days = old_version_days, deleted_grace_days

    async def run(self, tenant_id: str) -> dict:
        counts = {"versions_cleaned": 0, "documents_purged": 0}
        async with self.db.scoped(tenant_id) as c:
            old = await (await c.execute(
                "SELECT v.id, v.document_id, d.course_id FROM document_version v JOIN document d ON d.tenant_id = v.tenant_id AND d.id = v.document_id "
                "WHERE v.tenant_id = app_tenant() AND v.retired_at IS NOT NULL AND v.retired_at <= now() - make_interval(secs => %s) "
                "AND d.current_version_id IS DISTINCT FROM v.id AND EXISTS (SELECT 1 FROM chunk k WHERE k.tenant_id = v.tenant_id AND k.version_id = v.id)",
                (self.old_days * 86400,))).fetchall()
            doomed = await (await c.execute(
                "SELECT id, course_id FROM document WHERE tenant_id = app_tenant() AND status = 'deleted' "
                "AND deleted_at <= now() - make_interval(secs => %s)", (self.grace_days * 86400,))).fetchall()
        for version_id, document_id, course_id in old:
            await self.index.delete_version(tenant_id, str(course_id), str(document_id), str(version_id))  # index first: Postgres is the record
            async with self.db.scoped(tenant_id) as c:
                await c.execute("DELETE FROM chunk WHERE tenant_id = app_tenant() AND version_id = %s", (version_id,))
            counts["versions_cleaned"] += 1
        for document_id, course_id in doomed:
            await self.index.remove_document(tenant_id, str(course_id), str(document_id))
            async with self.db.scoped(tenant_id) as c:
                keys = [r[0] for r in await (await c.execute("SELECT object_key FROM document_version WHERE tenant_id = app_tenant() AND document_id = %s",
                                                              (document_id,))).fetchall()]
            for key in keys:
                await self.objects.delete(key)
            async with self.db.scoped(tenant_id) as c:
                await c.execute("DELETE FROM chunk WHERE tenant_id = app_tenant() AND document_id = %s", (document_id,))
                await c.execute("UPDATE document SET current_version_id = NULL WHERE tenant_id = app_tenant() AND id = %s", (document_id,))
                await c.execute("DELETE FROM document_version WHERE tenant_id = app_tenant() AND document_id = %s", (document_id,))
                await c.execute("DELETE FROM document WHERE tenant_id = app_tenant() AND id = %s", (document_id,))
                await c.execute("INSERT INTO audit_log (tenant_id, action, target) VALUES (app_tenant(), 'document.purged', %s)", (f"document:{document_id}",))
            counts["documents_purged"] += 1
        return counts
