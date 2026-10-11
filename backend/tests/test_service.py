"""The assembled service end to end: JWT auth, Postgres roles, upload -> worker -> chat, metrics and readiness."""
import asyncio
import os
import sys
import time
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL and psycopg are required")
try:
    import httpx
    import jwt
except ImportError as exc:
    raise unittest.SkipTest(f"extras missing: {exc}")

from studybuddy.auth import StaticJwks
from studybuddy.service import ConfigError, Settings, create_service
from tests.test_auth import AUD, ISS, JWK, KEY
from tests.test_rls import World

ENV = {"STUDYBUDDY_JWT_ISSUER": ISS, "STUDYBUDDY_JWT_AUDIENCE": AUD, "STUDYBUDDY_FAKE_MODEL": "1", "STUDYBUDDY_SCRAPE_TOKEN": "scrape"}


def settings(tenants=(), **extra):
    env = dict(ENV, **extra)
    for r in ("app", "auth", "worker", "relay"):
        env[f"STUDYBUDDY_DSN_{r.upper()}"] = pg_harness.dsn_for(f"sb_{r}")
    env["STUDYBUDDY_TENANTS"] = ",".join(tenants)
    return Settings.from_env(env)


class SettingsTests(unittest.TestCase):
    def test_missing_settings_are_named_and_no_model_fails_closed(self):
        with self.assertRaises(ConfigError) as cm:
            Settings.from_env({})
        self.assertIn("STUDYBUDDY_DSN_APP", str(cm.exception))
        s = Settings.from_env({**{f"STUDYBUDDY_DSN_{r}": "x" for r in ("APP", "AUTH", "WORKER", "RELAY")}, "STUDYBUDDY_JWT_ISSUER": "i", "STUDYBUDDY_JWT_AUDIENCE": "a"})
        with self.assertRaises(ConfigError):
            asyncio.run(create_service(s, jwks=StaticJwks([JWK])))   # no model key and no fake flag: refuse before connecting


class TenantMapSettingTests(unittest.TestCase):
    base = {**{f"STUDYBUDDY_DSN_{r}": "x" for r in ("APP", "AUTH", "WORKER", "RELAY")}, "STUDYBUDDY_JWT_ISSUER": "i", "STUDYBUDDY_JWT_AUDIENCE": "a"}

    def test_parsed_and_malformed_rejected(self):
        s = Settings.from_env({**self.base, "STUDYBUDDY_TENANT_MAP": "dir-1=t-1, dir-2=t-2"})
        self.assertEqual(dict(s.tenant_map), {"dir-1": "t-1", "dir-2": "t-2"})
        self.assertEqual(dict(Settings.from_env(self.base).tenant_map), {})
        for bad in ("dir-1", "=t", "dir-1="):
            with self.assertRaises(ConfigError):
                Settings.from_env({**self.base, "STUDYBUDDY_TENANT_MAP": bad})


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def setUp(self):
        self.w = World()
        self.A, self.B = self.w.t["A"], self.w.t["B"]

    def tok(self, tenant, sub, roles=("student",)):
        now = int(time.time())
        return jwt.encode({"iss": ISS, "aud": AUD, "sub": sub, "iat": now, "exp": now + 600, "tenant_id": tenant["id"],
                           "roles": list(roles), "jti": uuid.uuid4().hex}, KEY, algorithm="RS256", headers={"kid": "k1"})

    def run_service(self, fn):
        async def main():
            svc = await create_service(settings([self.A["id"]]), jwks=StaticJwks([JWK]))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=svc.app), base_url="http://t") as client:
                    return await fn(svc, client)
            finally:
                await svc.stop()
        return asyncio.run(main())

    def test_upload_then_ask_and_observe(self):
        A = self.A
        instr = {"authorization": f"Bearer {self.tok(A, 'sub-A-instr', ('instructor',))}"}
        stud = {"authorization": f"Bearer {self.tok(A, 'sub-A-s1')}"}

        async def go(svc, client):
            self.assertEqual((await client.get("/readyz")).status_code, 200)
            self.assertEqual((await client.post("/chat", json={"question": "q", "course_id": A["c1"], "request_id": "request-0000"})).status_code, 401)
            up = await client.post(f"/courses/{A['c1']}/documents", content=b"# Notes\n\nBCNF is stricter than 3NF.\n",
                                   headers={**instr, "x-filename": "notes.md"})
            self.assertEqual(up.status_code, 202, up.text)
            await svc.runner.tick()
            self.assertEqual(svc.errors, [])
            r = await client.post("/chat", json={"question": "What is BCNF?", "course_id": A["c1"], "request_id": "request-0001"}, headers=stud)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("event: done", r.text)
            await asyncio.sleep(0.1)
            self.assertEqual(svc.registry.get("sb_requests_total").value(tenant=A["id"], outcome="ok"), 1)
            self.assertEqual(len(svc.exporter.traces) <= svc.settings.max_traces, True)
            # another tenant's student cannot see the document: no notes -> not found, never A's text
            other = {"authorization": f"Bearer {self.tok(self.B, 'sub-B-s1')}"}
            rb = await client.post("/chat", json={"question": "What is BCNF?", "course_id": self.B["c1"], "request_id": "request-0002"}, headers=other)
            self.assertNotIn("stricter than 3NF", rb.text)
            # metrics: platform scrape needs the token; a tenant admin sees only their tenant
            self.assertEqual((await client.get("/metrics")).status_code, 404)
            m = await client.get("/metrics", headers={"authorization": "Bearer scrape"})
            self.assertIn("sb_requests_total", m.text)
            admin = {"authorization": f"Bearer {self.tok(A, 'sub-A-admin', ('tenant_admin',))}"}
            tv = await client.get("/tenant/metrics", headers=admin)
            self.assertEqual(tv.status_code, 200)
            self.assertIn(A["id"], tv.text)
            self.assertNotIn(self.B["id"], tv.text)
            self.assertEqual(svc.alerts.evaluate(), [])
        self.run_service(go)

    def test_background_loops_start_and_stop_cleanly(self):
        async def go(svc, client):
            svc.start()
            await asyncio.sleep(0.2)
            self.assertEqual(svc.errors, [])
        self.run_service(go)


if __name__ == "__main__":
    unittest.main()


def _azurite():
    import shutil, socket, subprocess, tempfile
    exe = os.environ.get("AZURITE_BIN") or shutil.which("azurite-blob")
    if not exe:
        return None
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    d = tempfile.mkdtemp()
    proc = subprocess.Popen([exe, "--silent", "--skipApiVersionCheck", "--blobHost", "127.0.0.1", "--blobPort", str(port), "--location", d],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    conn = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
            f"BlobEndpoint=http://127.0.0.1:{port}/devstoreaccount1;")
    return proc, conn


class AzureBlobTests(ServiceTests):
    """The same end-to-end flow, with uploads going to a (local Azurite) Azure Blob container."""

    test_background_loops_start_and_stop_cleanly = None

    @classmethod
    def setUpClass(cls):
        try:
            import azure.storage.blob.aio  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("azure-storage-blob not installed")
        started = _azurite()
        if not started:
            raise unittest.SkipTest("azurite-blob not available")
        cls.proc, cls.conn = started
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()

    def run_service(self, fn):
        async def main():
            s = settings([self.A["id"]], STUDYBUDDY_BLOB_CONNECTION_STRING=self.conn, STUDYBUDDY_BLOB_CONTAINER="docs-test")
            svc = await create_service(s, jwks=StaticJwks([JWK]))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=svc.app), base_url="http://t") as client:
                    out = await fn(svc, client)
                    names = [b.name async for b in svc.closers[0].container.list_blobs()]
                    self.assertTrue(any(n.endswith("notes.md") or self.A["id"] in n for n in names), names)
                    return out
            finally:
                await svc.stop()
        return asyncio.run(main())

    def test_store_contract(self):
        from studybuddy.ingestion import AzureBlobObjectStore

        async def main():
            st = AzureBlobObjectStore.from_connection_string(self.conn, "contract")
            await st.ensure_container(); await st.ensure_container()      # idempotent
            await st.put("t/c/d/v/file.bin", b"\x00\x01data"); await st.put("t/c/d/v/file.bin", b"second")
            self.assertEqual(await st.get("t/c/d/v/file.bin"), b"second")
            await st.delete("t/c/d/v/file.bin"); await st.delete("t/c/d/v/file.bin")   # delete twice is fine
            with self.assertRaises(FileNotFoundError):
                await st.get("t/c/d/v/file.bin")
            await st.close()
        asyncio.run(main())
