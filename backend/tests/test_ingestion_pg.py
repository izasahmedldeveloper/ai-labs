"""Ingestion against a real PostgreSQL: upload transaction, outbox relay, worker, swap, delete, cleanup (LLD 04 s6, s7)."""
import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL and psycopg are required")

from studybuddy.db import Database
from studybuddy.ingestion import (AccessDenied, CleanupJob, DocumentService, IngestionConsumer, IngestionError, IngestionWorker,
                                  InMemoryBroker, InMemoryObjectStore, MemoryIndex, OutboxRelay, PgVersionRepo)
from studybuddy.pipeline import Identity
from studybuddy.retrieval.types import SearchFilter
from tests.test_ingestion import Embedder
from tests.test_rls import World, sup

MD1 = b"# Notes\n\nOLD answer about zebras.\n"
MD2 = b"# Notes\n\nNEW answer about zebras.\n"


class PgIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def setUp(self):
        self.w = World()
        self.A, self.B = self.w.t["A"], self.w.t["B"]
        self.objects, self.index, self.broker, self.emb = InMemoryObjectStore(), MemoryIndex(), InMemoryBroker(), Embedder()

    def ident(self, tenant, key):
        t, u, r = self.w.who(tenant, key)
        return Identity(t, u, r)

    def run_all(self, fn):
        async def main():
            dbs = {r: await Database.connect(pg_harness.dsn_for(r), max_size=3) for r in ("sb_app", "sb_worker", "sb_relay")}
            try:
                return await fn(dbs)
            finally:
                for d in dbs.values():
                    await d.close()
        return asyncio.run(main())

    def stack(self, dbs):
        svc = DocumentService(dbs["sb_app"], self.objects)
        worker = IngestionWorker(PgVersionRepo(dbs["sb_worker"]), self.objects, self.index, self.emb, self.broker, sleep=self._nosleep)
        return svc, worker, OutboxRelay(dbs["sb_relay"], self.broker), IngestionConsumer(self.broker, worker)

    async def _nosleep(self, s):
        pass

    def visible(self, q="zebras"):
        return asyncio.run(self.avisible(q))

    async def avisible(self, q="zebras"):
        return [h.chunk.text for h in await self.index.store.sparse_search(SearchFilter(self.A["id"], self.A["c1"]), q, 10)]

    def one(self, sql, params=()):
        with sup() as c:
            return c.execute(sql, params).fetchall()

    # ------------------------------------------------------------------------------------------------ upload
    def test_upload_writes_version_event_audit_and_object_in_one_go(self):
        async def go(dbs):
            svc, *_ = self.stack(dbs)
            r = await svc.upload(self.ident("A", "instr"), self.A["c1"], "notes.md", MD1)
            self.assertEqual((r.version_no, r.status), (1, "processing"))
            return r
        r = self.run_all(go)
        self.assertEqual(self.one("SELECT status, object_key FROM document_version WHERE id = %s", (r.version_id,))[0][0], "uploaded")
        ev = self.one("SELECT type, payload->>'version_id' FROM outbox_event WHERE type LIKE 'document.%%'")
        self.assertEqual(ev, [("document.uploaded", r.version_id)])
        self.assertIn("document.upload", [a[0] for a in self.one("SELECT action FROM audit_log")])
        self.assertEqual(len(self.objects.objects), 1)

    def test_who_may_upload(self):
        async def go(dbs):
            svc, *_ = self.stack(dbs)
            for who, tenant_course in (("s1", "c1"), ("tutor", "c1")):
                with self.assertRaises(AccessDenied):
                    await svc.upload(self.ident("A", who), self.A[tenant_course], "n.md", MD1)
            with self.assertRaises(AccessDenied):                        # instructor of course c1 only; c2 belongs to nobody
                await svc.upload(self.ident("A", "instr"), self.A["c2"], "n.md", MD1)
            with self.assertRaises(AccessDenied):                        # other tenant's instructor on A's course
                await svc.upload(self.ident("B", "instr"), self.A["c1"], "n.md", MD1)
            ok = await svc.upload(self.ident("A", "admin"), self.A["c2"], "n.md", MD1)   # admins manage any course of their tenant
            self.assertEqual(ok.version_no, 1)
            # replacing needs the same rights, and a document id from another tenant is simply "not allowed"
            first = await svc.upload(self.ident("A", "instr"), self.A["c1"], "n.md", MD1)
            with self.assertRaises(AccessDenied):
                await svc.upload(self.ident("B", "instr"), self.B["c1"], "n.md", MD2, document_id=first.document_id)
        self.run_all(go)
        self.assertEqual(len(self.objects.objects), 2)

    def test_bad_files_are_refused_before_anything_is_stored(self):
        before = self.one("SELECT count(*) FROM document_version")[0][0]

        async def go(dbs):
            svc, *_ = self.stack(dbs)
            for data, name in ((b"%PDF-1.4 x", "a.md"), (b"", "a.md"), (b"data", "a.exe")):
                with self.assertRaises(IngestionError):
                    await svc.upload(self.ident("A", "instr"), self.A["c1"], name, data)
        self.run_all(go)
        self.assertEqual((len(self.objects.objects), self.one("SELECT count(*) FROM document_version")[0][0] - before), (0, 0))

    # ------------------------------------------------------------------------------------------------ end to end
    def test_upload_relay_worker_end_to_end_and_cache_event_shape(self):
        async def go(dbs):
            svc, worker, relay, consumer = self.stack(dbs)
            r = await svc.upload(self.ident("A", "instr"), self.A["c1"], "notes.md", MD1)
            self.assertEqual(await relay.run_once(), 1)
            self.assertEqual(await relay.run_once(), 0)                    # already marked
            self.assertEqual(await consumer.run_once(), 1)
            st = (await svc.status(self.ident("A", "instr"), r.document_id))[0]
            self.assertEqual((st["status"], st["progress"], st["flagged"]), ("ready", 100, False))
            self.assertEqual(await self.avisible(), ["OLD answer about zebras."])
            await relay.run_once()
            changed = [d.message for d in self.broker.published if d.topic == "document.changed"]
            self.assertEqual([(m["kind"], m["document_id"]) for m in changed], [("uploaded", r.document_id)])
            from studybuddy.cache.cdc import CdcConsumer

            class Cache:
                def __init__(self): self.calls = []
                async def invalidate_document(self, t, c, d): self.calls.append((t, c, d)); return 1
            cache = Cache()
            await CdcConsumer(cache).handle(changed[0])
            self.assertEqual(cache.calls, [(self.A["id"], self.A["c1"], r.document_id)])
            return r
        r = self.run_all(go)
        self.assertEqual(self.one("SELECT count(*) FROM chunk WHERE document_id = %s", (r.document_id,))[0][0], 1)
        self.assertEqual(str(self.one("SELECT current_version_id FROM document WHERE id = %s", (r.document_id,))[0][0]), r.version_id)

    def test_replace_swaps_and_retires_old_then_cleanup_removes_old_chunks(self):
        async def go(dbs):
            svc, worker, relay, consumer = self.stack(dbs)
            who = self.ident("A", "instr")
            r1 = await svc.upload(who, self.A["c1"], "notes.md", MD1)
            await relay.run_once(); await consumer.run_once()
            r2 = await svc.upload(who, self.A["c1"], "notes.md", MD2, document_id=r1.document_id)
            self.assertEqual(r2.version_no, 2)
            self.assertEqual(await self.avisible(), ["OLD answer about zebras."])    # students still see the old one while v2 processes
            await relay.run_once(); await consumer.run_once()
            self.assertEqual(await self.avisible(), ["NEW answer about zebras."])
            self.assertEqual(self.one("SELECT retired_at IS NOT NULL FROM document_version WHERE id = %s", (r1.version_id,))[0][0], True)
            job = CleanupJob(dbs["sb_worker"], self.index, self.objects, old_version_days=7)
            self.assertEqual((await job.run(self.A["id"]))["versions_cleaned"], 0)          # inside the 7-day window
            job = CleanupJob(dbs["sb_worker"], self.index, self.objects, old_version_days=0)
            self.assertEqual((await job.run(self.A["id"]))["versions_cleaned"], 1)
            self.assertEqual((await job.run(self.A["id"]))["versions_cleaned"], 0)          # idempotent
            self.assertEqual(await self.avisible(), ["NEW answer about zebras."])
            return r1
        r1 = self.run_all(go)
        self.assertEqual(self.one("SELECT count(*) FROM chunk WHERE version_id = %s", (r1.version_id,))[0][0], 0)
        self.assertEqual(self.one("SELECT count(*) FROM chunk WHERE document_id = %s", (r1.document_id,))[0][0], 1)

    def test_older_version_finishing_late_never_replaces_the_newer_one(self):
        async def go(dbs):
            svc, worker, relay, consumer = self.stack(dbs)
            who = self.ident("A", "instr")
            r1 = await svc.upload(who, self.A["c1"], "n.md", MD1)
            r2 = await svc.upload(who, self.A["c1"], "n.md", MD2, document_id=r1.document_id)
            await relay.run_once()
            msgs = [d.message for d in self.broker.topics["document.uploaded"]]
            by_no = {m["version_id"]: m for m in msgs}
            await worker.handle(by_no[r2.version_id])
            await worker.handle(by_no[r1.version_id])                                  # v1 arrives second
            self.assertEqual(await self.avisible(), ["NEW answer about zebras."])
            return r1, r2
        r1, r2 = self.run_all(go)
        self.assertEqual(str(self.one("SELECT current_version_id FROM document WHERE id = %s", (r1.document_id,))[0][0]), r2.version_id)
        self.assertEqual(self.one("SELECT count(*) FROM outbox_event WHERE type = 'document.changed' AND payload->>'document_id' = %s",
                                  (r1.document_id,))[0][0], 1)

    def test_delete_hides_now_cleans_index_and_purges_after_grace(self):
        async def go(dbs):
            svc, worker, relay, consumer = self.stack(dbs)
            who = self.ident("A", "instr")
            r = await svc.upload(who, self.A["c1"], "notes.md", MD1)
            await relay.run_once(); await consumer.run_once()
            with self.assertRaises(AccessDenied):
                await svc.delete(self.ident("A", "s1"), r.document_id)
            with self.assertRaises(AccessDenied):
                await svc.delete(self.ident("B", "admin"), r.document_id)
            await svc.delete(who, r.document_id)
            await svc.delete(who, r.document_id)                                # twice is fine
            await relay.run_once(); await consumer.run_once()
            self.assertEqual(await self.avisible(), [])
            kinds = [d.message["kind"] for d in self.broker.published if d.topic == "document.changed" and d.message.get("kind") == "deleted"]
            self.assertEqual(len(kinds), 1)
            self.assertEqual(await CleanupJob(dbs["sb_worker"], self.index, self.objects, deleted_grace_days=30).run(self.A["id"]),
                             {"versions_cleaned": 0, "documents_purged": 0})
            self.assertEqual((await CleanupJob(dbs["sb_worker"], self.index, self.objects, deleted_grace_days=0).run(self.A["id"]))["documents_purged"], 1)
            return r
        r = self.run_all(go)
        self.assertEqual([self.one(f"SELECT count(*) FROM {t} WHERE {col} = %s", (r.document_id,))[0][0]
                          for t, col in (("document", "id"), ("document_version", "document_id"), ("chunk", "document_id"))], [0, 0, 0])
        self.assertEqual(self.objects.objects, {})
        self.assertIn("document.purged", [a[0] for a in self.one("SELECT action FROM audit_log")])

    def test_upload_during_delete_does_not_resurrect_the_document(self):
        async def go(dbs):
            svc, worker, relay, consumer = self.stack(dbs)
            who = self.ident("A", "instr")
            r = await svc.upload(who, self.A["c1"], "notes.md", MD1)
            await svc.delete(who, r.document_id)                              # deleted before the worker ran
            await relay.run_once(); await consumer.run_once()
            st = (await svc.status(who, r.document_id))[0]
            self.assertEqual(st["status"], "failed")
            self.assertEqual(await self.avisible(), [])
        self.run_all(go)

    # ------------------------------------------------------------------------------------------------ isolation and roles
    def test_worker_cannot_load_another_tenants_version(self):
        async def go(dbs):
            svc, *_ = self.stack(dbs)
            r = await svc.upload(self.ident("A", "instr"), self.A["c1"], "n.md", MD1)
            repo = PgVersionRepo(dbs["sb_worker"])
            self.assertIsNotNone(await repo.load(self.A["id"], r.version_id))
            self.assertIsNone(await repo.load(self.B["id"], r.version_id))
        self.run_all(go)

    def test_relay_role_is_narrow(self):
        import psycopg
        from psycopg import errors
        async def go(dbs):
            svc, *_ = self.stack(dbs)
            await svc.upload(self.ident("A", "instr"), self.A["c1"], "n.md", MD1)
            await svc.upload(self.ident("B", "instr"), self.B["c1"], "n.md", MD1)
        self.run_all(go)
        with psycopg.connect(pg_harness.dsn_for("sb_relay")) as c:
            self.assertEqual(c.execute("SELECT count(DISTINCT tenant_id) FROM outbox_event").fetchone()[0], 2)   # sees every tenant's events...
            for sql in ("SELECT * FROM chunk", "SELECT * FROM message", "SELECT * FROM app_user", "SELECT * FROM document",
                        "INSERT INTO outbox_event (tenant_id, type, payload) VALUES (gen_random_uuid(), 'x', '{}')",
                        "UPDATE outbox_event SET payload = '{}'", "DELETE FROM outbox_event"):
                with self.assertRaises(errors.InsufficientPrivilege, msg=sql):                                    # ...and nothing else
                    c.execute(sql)
                c.rollback()
            with self.assertRaises(psycopg.errors.CheckViolation if False else psycopg.Error):
                c.execute("UPDATE outbox_event SET published_at = NULL")


if __name__ == "__main__":
    unittest.main()
