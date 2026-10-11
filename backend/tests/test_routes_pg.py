"""HTTP routes and the worker runner, end to end on real PostgreSQL."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL and psycopg are required")
try:
    import httpx
    from fastapi import FastAPI
except ImportError as exc:
    raise unittest.SkipTest(f"API extras missing: {exc}")

from studybuddy.api import GatewayHeaderAuth
from studybuddy.api.routes import add_routes
from studybuddy.cache import InMemoryCacheStore, SemanticCache
from studybuddy.db import ConversationRepo, Database
from studybuddy.escalation import FaqService, InMemoryChannel, Notifier, PgEscalation, SlaMonitor, TicketService
from studybuddy.guardrails import RuleInputGuardrails
from studybuddy.ingestion import (CleanupJob, DocumentService, IngestionConsumer, IngestionWorker, InMemoryBroker, InMemoryObjectStore,
                                  Limits, MemoryIndex, OutboxRelay, PgVersionRepo)
from studybuddy.retrieval.fakes import HashingEmbedder
from studybuddy.retrieval.types import SearchFilter
from studybuddy.workers import WorkerRunner
from tests.test_escalation import Versions
from tests.test_rls import World, new, sup

MD = b"# Notes\n\nBCNF is stricter than 3NF.\n"


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def setUp(self):
        self.w = World()
        self.A, self.B = self.w.t["A"], self.w.t["B"]
        A = self.A
        with sup() as c:
            A["tutor2"] = new()
            c.execute("INSERT INTO app_user (id, tenant_id, idp_subject, role) VALUES (%s,%s,'sub-t2','tutor')", (A["tutor2"], A["id"]))
            c.execute("INSERT INTO enrollment VALUES (%s,%s,%s,'tutor')", (A["id"], A["c1"], A["tutor2"]))
            c.execute("DELETE FROM escalation_ticket")

    def hdr(self, key, tenant="A"):
        t = self.w.t[tenant]
        role = "tutor" if key.startswith("tutor") else {"admin": "tenant_admin", "instr": "instructor"}.get(key, "student")
        return {"x-tenant-id": t["id"], "x-user-id": t[key], "x-user-role": role}

    def run_app(self, fn, limits=None):
        async def main():
            dbs = {r: await Database.connect(pg_harness.dsn_for(r), max_size=4) for r in ("sb_app", "sb_worker", "sb_relay")}
            try:
                objects, index, broker = InMemoryObjectStore(), MemoryIndex(), InMemoryBroker()
                san = RuleInputGuardrails().sanitize
                esc = PgEscalation(dbs["sb_app"])
                cache = SemanticCache(InMemoryCacheStore(), HashingEmbedder(), Versions(), policy_version=lambda: "p1")
                app = FastAPI()
                add_routes(app, GatewayHeaderAuth(), documents=DocumentService(dbs["sb_app"], objects), tickets=TicketService(dbs["sb_app"], esc, san),
                           faq=FaqService(dbs["sb_app"], cache, san), conversations=ConversationRepo(dbs["sb_app"]), limits=limits)
                worker = IngestionWorker(PgVersionRepo(dbs["sb_worker"]), objects, index, HashingEmbedder(), broker)
                chan = InMemoryChannel()
                errors = []
                runner = WorkerRunner(relay=OutboxRelay(dbs["sb_relay"], broker), broker=broker, ingestion_consumer=IngestionConsumer(broker, worker),
                                      notifier=Notifier(dbs["sb_worker"], [chan]), sla=SlaMonitor(dbs["sb_worker"]),
                                      cleanup=CleanupJob(dbs["sb_worker"], index, objects), tenants=lambda: _tenants(self.A["id"]),
                                      periodic_every_s=0, on_error=lambda w, e: errors.append((w, e)))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    return await fn(client, runner, index, chan, errors, broker)
            finally:
                for d in dbs.values():
                    await d.close()
        return asyncio.run(main())

    def test_document_lifecycle_over_http(self):
        async def go(client, runner, index, chan, errors, broker):
            up = lambda who, **kw: client.post(f"/courses/{self.A['c1']}/documents", content=kw.pop("data", MD),
                                                headers={**self.hdr(who), **kw.pop("headers", {"x-filename": "notes.md"})}, **kw)
            self.assertEqual((await up("s1")).status_code, 404)                                       # a student may not upload
            self.assertEqual((await up("instr", headers={})).status_code, 422)                         # no filename
            self.assertEqual((await up("instr", headers={"x-filename": "a.exe"})).status_code, 422)
            self.assertEqual((await up("instr", data=b"%PDF-1.4 x", headers={"x-filename": "a.md"})).status_code, 422)
            r = await up("instr")
            self.assertEqual((r.status_code, r.json()["status"]), (202, "processing"))
            doc = r.json()["document_id"]
            self.assertEqual((await client.get(f"/documents/{doc}/status", headers=self.hdr("instr"))).json()["versions"][0]["status"], "uploaded")
            await runner.tick()
            st = (await client.get(f"/documents/{doc}/status", headers=self.hdr("instr"))).json()["versions"][0]
            self.assertEqual((st["status"], st["progress"]), ("ready", 100))
            hits = await index.store.sparse_search(SearchFilter(self.A["id"], self.A["c1"]), "BCNF", 5)
            self.assertEqual(len(hits), 1)
            self.assertEqual((await client.get(f"/documents/{doc}/status", headers=self.hdr("instr", "B"))).status_code, 404)   # other tenant
            self.assertEqual((await client.delete(f"/documents/{doc}", headers=self.hdr("s1"))).status_code, 404)
            self.assertEqual((await client.delete(f"/documents/{doc}", headers=self.hdr("instr"))).status_code, 204)
            await runner.tick()
            self.assertEqual(await index.store.sparse_search(SearchFilter(self.A["id"], self.A["c1"]), "BCNF", 5), [])
            self.assertEqual(errors, [])
        self.run_app(go)

    def test_upload_size_limit(self):
        async def go(client, runner, index, chan, errors, broker):
            r = await client.post(f"/courses/{self.A['c1']}/documents", content=b"x" * 600, headers={**self.hdr("instr"), "x-filename": "a.md"})
            self.assertEqual(r.status_code, 413)
        self.run_app(go, Limits(max_bytes=500))

    def test_ticket_lifecycle_over_http(self):
        async def go(client, runner, index, chan, errors, broker):
            s2, t1, t2 = self.hdr("s2"), self.hdr("tutor"), self.hdr("tutor2")
            r = await client.post(f"/courses/{self.A['c1']}/tickets", json={"note": "Please look at my grade"}, headers=s2)
            self.assertEqual(r.status_code, 201)
            tid = r.json()["ticket_id"]
            self.assertTrue((await client.post(f"/courses/{self.A['c1']}/tickets", json={}, headers=s2)).json()["already_open"])
            self.assertEqual((await client.post(f"/courses/{self.A['c1']}/tickets", json={"x": 1}, headers=s2)).status_code, 422)
            await runner.tick()
            self.assertEqual({s[1] for s in chan.sent}, {self.A["tutor"], self.A["tutor2"]})
            self.assertEqual((await client.get("/tutor/queue", headers=s2)).status_code, 404)             # students have no queue
            q = (await client.get("/tutor/queue", headers=t2)).json()["tickets"]
            self.assertEqual([x["ticket_id"] for x in q], [tid])
            self.assertEqual((await client.get(f"/tutor/tickets/{tid}/conversation", headers=t2)).status_code, 404)   # not claimed yet
            self.assertEqual((await client.post(f"/tutor/tickets/{tid}/claim", headers=t2)).status_code, 200)
            self.assertEqual((await client.post(f"/tutor/tickets/{tid}/claim", headers=t1)).status_code, 409)
            conv = (await client.get(f"/tutor/tickets/{tid}/conversation", headers=t2)).json()["messages"]
            self.assertIn("Please look at my grade", [m["text"] for m in conv])
            self.assertEqual((await client.get(f"/tutor/tickets/{tid}/conversation", headers=t1)).status_code, 404)
            self.assertEqual((await client.post(f"/tickets/{tid}/replies", json={"body": "Which module?"}, headers=t2)).status_code, 201)
            self.assertEqual((await client.post(f"/tickets/{tid}/replies", json={"body": "hi"}, headers=self.hdr("s1"))).status_code, 404)
            view = (await client.get(f"/tickets/{tid}", headers=s2)).json()
            self.assertEqual((view["status"], view["replies"][0]["from"]), ("in_progress", "Tutor"))
            self.assertEqual((await client.get(f"/tickets/{tid}", headers=self.hdr("s1"))).status_code, 404)
            bad = await client.post(f"/tutor/tickets/{tid}/resolve", json={"outcome": "nope", "resolution": "x"}, headers=t2)
            self.assertEqual(bad.status_code, 422)
            ok = await client.post(f"/tutor/tickets/{tid}/resolve", json={"outcome": "answered", "resolution": "Done."}, headers=t2)
            self.assertEqual(ok.status_code, 200)
            faq = await client.post(f"/tutor/tickets/{tid}/faq", json={"question": "How do I query my grade?", "answer": "Use the grade query form."}, headers=t2)
            self.assertEqual((faq.status_code, faq.json()["cached"]), (201, True))
            pii = await client.post(f"/tutor/tickets/{tid}/faq", json={"question": "q?", "answer": "mail ann@example.com"}, headers=t2)
            self.assertEqual(pii.status_code, 422)
            self.assertEqual((await client.post(f"/tickets/{tid}/reopen", headers=s2)).status_code, 200)
            self.assertEqual((await client.post(f"/tickets/{tid}/reopen", headers=s2)).status_code, 409)
            self.assertEqual((await client.post(f"/tickets/{tid}/cancel", headers=s2)).status_code, 200)
            self.assertEqual([t["status"] for t in (await client.get("/tickets", headers=s2)).json()["tickets"]], ["closed"])
            await runner.tick()
            kinds = {s[2]["kind"] for s in chan.sent}
            self.assertTrue({"created", "claimed", "reply", "resolved", "reopened"} <= kinds, kinds)
            self.assertEqual(errors, [])
        self.run_app(go)

    def test_runner_isolates_failures(self):
        async def go(client, runner, index, chan, errors, broker):
            r = await client.post(f"/courses/{self.A['c1']}/documents", content=MD, headers={**self.hdr("instr"), "x-filename": "n.md"})
            doc = r.json()["document_id"]

            async def boom():
                raise RuntimeError("kafka down")
            real = runner.relay.run_once
            runner.relay.run_once = boom
            out = await runner.tick()
            self.assertEqual(out["relayed"], 0)
            self.assertEqual([w for w, _ in errors], ["relay"])
            runner.relay.run_once = real
            await runner.tick()
            st = (await client.get(f"/documents/{doc}/status", headers=self.hdr("instr"))).json()["versions"][0]["status"]
            self.assertEqual(st, "ready")                                                            # nothing was lost
        self.run_app(go)


async def _tenants(tenant):
    return [tenant]


if __name__ == "__main__":
    unittest.main()
