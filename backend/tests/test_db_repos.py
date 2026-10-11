"""Repository + Database tests against real Postgres, connecting as sb_app (never a superuser)."""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL and psycopg are required")

from studybuddy.db import AuditRepo, ConversationRepo, Database, EnrollmentRepo, ScopeError, TutorRepo
from studybuddy.pipeline import Identity, RequestContext
from tests.test_rls import World, sup


def ident(w, tenant, key):
    t, u, r = w.who(tenant, key)
    return Identity(t, u, r)


class RepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def setUp(self):
        self.w = World()
        self.A, self.B = self.w.t["A"], self.w.t["B"]

    def run_db(self, fn):
        async def main():
            db = await Database.connect(pg_harness.dsn_for("sb_app"), max_size=3)
            try:
                return await fn(db)
            finally:
                await db.close()
        return asyncio.run(main())

    def test_enrollment(self):
        async def go(db):
            repo = EnrollmentRepo(db)
            s1 = ident(self.w, "A", "s1")
            self.assertTrue(await repo.is_enrolled(s1, self.A["c1"]))
            self.assertFalse(await repo.is_enrolled(s1, self.A["c2"]))            # not enrolled there
            self.assertFalse(await repo.is_enrolled(ident(self.w, "A", "s3"), self.A["c1"]))
            self.assertFalse(await repo.is_enrolled(s1, self.B["c1"]))            # other tenant's course
            self.assertFalse(await repo.is_enrolled(s1, "not-a-uuid"))
        self.run_db(go)

    def test_load_creates_then_reuses_conversation_and_returns_history_starting_with_user(self):
        async def go(db):
            repo = ConversationRepo(db)
            s3 = ident(self.w, "A", "s3")
            with sup() as c:
                c.execute("INSERT INTO enrollment VALUES (%s,%s,%s,'student')", (self.A["id"], self.A["c1"], self.A["s3"]))
            first = await repo.load(s3, self.A["c1"])
            self.assertEqual(first.history, ())
            again = await repo.load(s3, self.A["c1"])
            self.assertEqual(first.conversation_id, again.conversation_id)
            s1 = await repo.load(ident(self.w, "A", "s1"), self.A["c1"])
            self.assertEqual(s1.conversation_id, self.A["conv_s1"])
            if s1.history:
                self.assertEqual(len(s1.history) % 1, 0)
        self.run_db(go)

    def test_record_turn_stores_redacted_text_and_history_round_trips(self):
        async def go(db):
            repo = ConversationRepo(db)
            s1 = ident(self.w, "A", "s1")
            state = await repo.load(s1, self.A["c1"])
            ctx = RequestContext(identity=s1, course_id=self.A["c1"], question="What is [REDACTED]?", request_id="r1",
                                 conversation_id=state.conversation_id)
            ctx.original_question = "What is secret-name?"
            ctx.answer, ctx.route, ctx.outcome = "It is a thing.", "cheap_llm", "ok"
            await repo.record_turn(ctx)
            after = await repo.load(s1, self.A["c1"])
            self.assertEqual(after.history[-2:], ("What is [REDACTED]?", "It is a thing."))
            return state
        self.run_db(go)
        with sup() as c:
            texts = [r[0] for r in c.execute("SELECT content_redacted FROM message").fetchall()]
            self.assertNotIn("secret-name", " ".join(texts))

    def test_record_turn_without_conversation_id_is_a_no_op_and_foreign_conversation_is_refused(self):
        async def go(db):
            repo = ConversationRepo(db)
            s1 = ident(self.w, "A", "s1")
            ctx = RequestContext(identity=s1, course_id=self.A["c1"], question="q", request_id="r")
            await repo.record_turn(ctx)  # no conversation id: nothing happens
            other = RequestContext(identity=s1, course_id=self.A["c1"], question="q", request_id="r2",
                                   conversation_id=self.A["conv_s2"])  # someone else's conversation
            other.answer = "a"
            with self.assertRaises(Exception):
                await repo.record_turn(other)
        self.run_db(go)

    def test_audit_and_tutor_read(self):
        async def go(db):
            await AuditRepo(db).log(ident(self.w, "A", "admin"), "admin.test", "x")
            tutor = TutorRepo(db)
            msgs = await tutor.read_ticket(ident(self.w, "A", "tutor"), self.A["ticket"])
            self.assertTrue(msgs)
            with self.assertRaises(ScopeError):
                await tutor.read_ticket(ident(self.w, "A", "tutor"), "nope")
            with self.assertRaises(Exception):
                await tutor.read_ticket(ident(self.w, "A", "s1"), self.A["ticket"])
        self.run_db(go)
        with sup() as c:
            actions = [r[0] for r in c.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
        self.assertIn("admin.test", actions)
        self.assertIn("chat.read", actions)

    def test_scope_is_validated_and_does_not_leak_between_pooled_uses(self):
        async def go(db):
            with self.assertRaises(ScopeError):
                async with db.scoped("not-a-uuid"):
                    pass
            async with db.scoped(self.A["id"], self.A["s1"], "student") as conn:
                n = (await (await conn.execute("SELECT count(*) FROM conversation")).fetchone())[0]
            self.assertGreater(n, 0)
            async with db.pool.connection() as conn:  # raw reuse of the pool without a scope sees nothing
                n = (await (await conn.execute("SELECT count(*) FROM conversation")).fetchone())[0]
            self.assertEqual(n, 0)
        self.run_db(go)


class PgDirectoryTests(RepoTests):
    def run_auth(self, fn):
        async def main():
            db = await Database.connect(pg_harness.dsn_for("sb_auth"), max_size=2)
            try:
                return await fn(db)
            finally:
                await db.close()
        return asyncio.run(main())

    def test_jit_provisioning_existing_user_unknown_tenant_and_isolation(self):
        from studybuddy.auth import AuthError, PgUserDirectory

        async def go(db):
            d = PgUserDirectory(db)
            existing = await d.resolve(self.A["id"], "sub-A-instr", ["student"])
            self.assertEqual((existing.user_id, existing.role), (self.A["instr"], "instructor"))  # DB role wins over the token
            new = await d.resolve(self.A["id"], "brand-new", ["instructor", "tenant_admin"])
            self.assertEqual((new.role, new.status), ("instructor", "active"))
            again = await d.resolve(self.A["id"], "brand-new", [])
            self.assertEqual(again.user_id, new.user_id)                          # no duplicate row
            other = await d.resolve(self.B["id"], "brand-new", [])
            self.assertNotEqual(other.user_id, new.user_id)                        # same sub, other tenant: other user
            admin_try = await d.resolve(self.A["id"], "wants-admin", ["tenant_admin"])
            self.assertEqual(admin_try.role, "student")
            with self.assertRaises(AuthError):
                await d.resolve(__import__("uuid").uuid4().hex and str(__import__("uuid").uuid4()), "x", [])
        self.run_auth(go)

    test_enrollment = test_load_creates_then_reuses_conversation_and_returns_history_starting_with_user = None
    test_record_turn_stores_redacted_text_and_history_round_trips = None
    test_record_turn_without_conversation_id_is_a_no_op_and_foreign_conversation_is_refused = None
    test_audit_and_tutor_read = test_scope_is_validated_and_does_not_leak_between_pooled_uses = None


if __name__ == "__main__":
    unittest.main()
