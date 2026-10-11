"""Row-level security and schema tests against a REAL PostgreSQL (LLD 01 s3 'mandatory test', LLD 02 s10).

Every test connects as an application role (sb_app, sb_auth, sb_worker, sb_eval), never as a superuser, because a
superuser bypasses RLS and would prove nothing. Fixtures are loaded as the superuser.
Skipped when no Postgres is available (see tests/pg_harness.py); do not ship without running them somewhere with one.
"""
import asyncio
import contextlib
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL (server binaries or TEST_DATABASE_URL) and psycopg are required")

import psycopg
from psycopg import errors

TABLES = ["tenant", "app_user", "course", "enrollment", "document", "document_version", "chunk", "conversation",
          "message", "message_raw", "escalation_ticket", "faq_entry", "usage_ledger", "audit_log", "outbox_event"]


def new() -> str:
    return str(uuid.uuid4())


def sup():
    return psycopg.connect(pg_harness.dsn_for("super"), autocommit=True)


@contextlib.contextmanager
def scoped(db_role, tenant=None, user=None, role=None):
    """A transaction as `db_role` scoped like studybuddy.db.session does; always rolled back."""
    with psycopg.connect(pg_harness.dsn_for(db_role)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true), set_config('app.role', %s, true)",
                     (tenant or "", user or "", role or ""))
        try:
            yield conn
        finally:
            conn.rollback()


class World:
    """Two tenants with IDENTICAL structure and content (the LLD 01 'mandatory test' setup)."""

    def __init__(self):
        self.t = {}
        with sup() as c:
            c.execute("TRUNCATE tenant, outbox_event, audit_log, usage_ledger, faq_entry, escalation_ticket, message_raw, message, "
                      "conversation, chunk, document_version, document, enrollment, course, app_user CASCADE")
            for name, retention in (("A", False), ("B", True)):
                self.t[name] = self._tenant(c, name, retention)

    def _tenant(self, c, name, retention):
        t = {"id": new()}
        c.execute("INSERT INTO tenant (id, name, eval_retention) VALUES (%s, %s, %s)", (t["id"], f"Uni {name}", retention))
        for key, role in (("admin", "tenant_admin"), ("instr", "instructor"), ("tutor", "tutor"), ("s1", "student"),
                          ("s2", "student"), ("s3", "student")):
            t[key] = new()
            c.execute("INSERT INTO app_user (id, tenant_id, idp_subject, role) VALUES (%s, %s, %s, %s)", (t[key], t["id"], f"sub-{name}-{key}", role))
        t["c1"], t["c2"] = new(), new()
        for course in ("c1", "c2"):
            c.execute("INSERT INTO course (id, tenant_id, title) VALUES (%s, %s, %s)", (t[course], t["id"], f"Databases {course}"))
        for key, role in (("instr", "instructor"), ("tutor", "tutor"), ("s1", "student"), ("s2", "student")):
            c.execute("INSERT INTO enrollment VALUES (%s, %s, %s, %s)", (t["id"], t["c1"], t[key], role))
        t["doc"], t["ver"], t["chunk"] = new(), new(), new()
        c.execute("INSERT INTO document (id, tenant_id, course_id, title) VALUES (%s, %s, %s, 'Syllabus')", (t["doc"], t["id"], t["c1"]))
        c.execute("INSERT INTO document_version (id, tenant_id, document_id, version_no, object_key, sha256, status) VALUES (%s,%s,%s,1,'k',%s,'ready')",
                  (t["ver"], t["id"], t["doc"], "a" * 64))
        c.execute("UPDATE document SET current_version_id = %s WHERE id = %s", (t["ver"], t["doc"]))
        c.execute("INSERT INTO chunk (id, tenant_id, course_id, document_id, version_id, ordinal, text, content_hash, token_count, embedding_model) "
                  "VALUES (%s,%s,%s,%s,%s,0,'BCNF text','h',2,'m1')", (t["chunk"], t["id"], t["c1"], t["doc"], t["ver"]))
        for who in ("s1", "s2"):
            conv = t[f"conv_{who}"] = new()
            c.execute("INSERT INTO conversation (id, tenant_id, user_id, course_id) VALUES (%s,%s,%s,%s)", (conv, t["id"], t[who], t["c1"]))
            for role, text in (("user", f"{who} secret question"), ("assistant", f"{who} answer")):
                mid = t[f"msg_{who}_{role}"] = new()
                c.execute("INSERT INTO message (id, tenant_id, conversation_id, role, content_redacted) VALUES (%s,%s,%s,%s,%s)",
                          (mid, t["id"], conv, role, text))
            c.execute("INSERT INTO message_raw (tenant_id, message_id, content) VALUES (%s,%s,%s)", (t["id"], t[f"msg_{who}_user"], f"{who} RAW text"))
        t["ticket"] = new()
        c.execute("INSERT INTO escalation_ticket (id, tenant_id, course_id, conversation_id, message_id, reason, status, assigned_tutor_id, student_id) "
                  "VALUES (%s,%s,%s,%s,%s,'grade','assigned',%s,%s)", (t["ticket"], t["id"], t["c1"], t["conv_s1"], t["msg_s1_assistant"], t["tutor"], t["s1"]))
        c.execute("INSERT INTO usage_ledger (tenant_id, user_id, day, tokens_in) VALUES (%s,%s,current_date,5)", (t["id"], t["s1"]))
        c.execute("INSERT INTO audit_log (tenant_id, actor_id, action) VALUES (%s,%s,'seed')", (t["id"], t["admin"]))
        c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (%s,'x','{}')", (t["id"],))
        return t

    def who(self, tenant, key):
        t = self.t[tenant]
        role = {"admin": "tenant_admin", "instr": "instructor", "tutor": "tutor"}.get(key, "student")
        return t["id"], t[key], role


class RlsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def setUp(self):
        self.w = World()
        self.A, self.B = self.w.t["A"], self.w.t["B"]

    def rows(self, sql, *who, db_role="sb_app", params=()):
        with scoped(db_role, *who) as c:
            return c.execute(sql, params).fetchall()


class RolesAndSchemaTests(RlsCase):
    def test_application_roles_cannot_bypass_rls(self):
        with sup() as c:
            rows = c.execute("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'sb_%'").fetchall()
        self.assertEqual(len(rows), 7)
        for name, is_super, bypass in rows:
            self.assertFalse(is_super or bypass, name)

    def test_every_tenant_table_forces_rls_and_has_policies(self):
        with sup() as c:
            for t in TABLES:
                enabled, forced = c.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", (t,)).fetchone()
                self.assertTrue(enabled and forced, t)
                n = c.execute("SELECT count(*) FROM pg_policies WHERE tablename = %s", (t,)).fetchone()[0]
                self.assertGreater(n, 0, t)

    def test_every_tenant_column_is_not_null_and_every_child_fk_includes_tenant(self):
        with sup() as c:
            nullable = c.execute("SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='tenant_id' AND is_nullable='YES'").fetchall()
            self.assertEqual(nullable, [])
            # composite foreign keys: no child table may reference another tenant-owned table by id alone
            single = c.execute("""
                SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint
                 WHERE contype='f' AND array_length(conkey,1)=1 AND confrelid::regclass::text <> 'tenant'""").fetchall()
            self.assertEqual(single, [])

    def test_uuid_v7_is_version_7_and_time_ordered(self):
        with sup() as c:
            ids = [c.execute("SELECT uuid_v7()").fetchone()[0] for _ in range(5)]
            self.assertTrue(all(i.version == 7 for i in ids))
            self.assertEqual(ids, sorted(ids, key=lambda u: u.int >> 80))  # millisecond prefix never goes backwards

    def test_a_row_cannot_reference_another_tenants_row_even_without_rls(self):
        with sup() as c:
            with self.assertRaises(errors.ForeignKeyViolation):
                c.execute("INSERT INTO enrollment VALUES (%s, %s, %s, 'student')", (self.A["id"], self.B["c1"], self.A["s3"]))
            with self.assertRaises(errors.ForeignKeyViolation):
                c.execute("INSERT INTO conversation (tenant_id, user_id, course_id) VALUES (%s,%s,%s)", (self.A["id"], self.B["s1"], self.A["c1"]))
            with self.assertRaises(errors.ForeignKeyViolation):
                c.execute("INSERT INTO message (tenant_id, conversation_id, role, content_redacted) VALUES (%s,%s,'user','x')", (self.A["id"], self.B["conv_s1"]))


class TenantIsolationTests(RlsCase):
    def test_no_scope_means_no_rows_anywhere(self):
        for role in ("sb_app", "sb_auth", "sb_worker", "sb_eval"):
            for table in TABLES:
                try:
                    got = self.rows(f"SELECT count(*) FROM {table}", db_role=role)
                except errors.InsufficientPrivilege:
                    continue
                self.assertEqual(got[0][0], 0, f"{role} read {table} without a tenant scope")

    def test_scoped_to_tenant_a_never_sees_tenant_b_in_any_table(self):
        for user in ("admin", "instr", "tutor", "s1"):
            who = self.w.who("A", user)
            for table in TABLES:
                try:
                    got = self.rows(f"SELECT DISTINCT tenant_id FROM {table}" if table != "tenant" else "SELECT id FROM tenant", *who)
                except errors.InsufficientPrivilege:
                    continue
                self.assertTrue(all(str(r[0]) == self.A["id"] for r in got), f"{user} saw another tenant in {table}")

    def test_fetching_tenant_b_rows_by_id_returns_nothing(self):
        who = self.w.who("A", "admin")
        for table, key in (("course", "c1"), ("document", "doc"), ("app_user", "s1"), ("conversation", "conv_s1"), ("escalation_ticket", "ticket")):
            self.assertEqual(self.rows(f"SELECT 1 FROM {table} WHERE id = %s", *who, params=(self.B[key],)), [], table)

    def test_writes_cannot_touch_or_create_other_tenants_rows(self):
        who = self.w.who("A", "admin")
        with scoped("sb_app", *who) as c:
            self.assertEqual(c.execute("UPDATE course SET title='pwned' WHERE id = %s", (self.B["c1"],)).rowcount, 0)
            self.assertEqual(c.execute("UPDATE app_user SET role='tenant_admin' WHERE id = %s", (self.B["s1"],)).rowcount, 0)
        with scoped("sb_app", *who) as c, self.assertRaises(errors.InsufficientPrivilege):  # RLS WITH CHECK violation
            c.execute("INSERT INTO course (tenant_id, title) VALUES (%s, 'planted')", (self.B["id"],))

    def test_a_tenant_id_cannot_be_rewritten(self):
        with sup() as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE course SET tenant_id = %s WHERE id = %s", (self.B["id"], self.A["c2"]))

    def test_scope_does_not_leak_through_a_reused_connection(self):
        async def go():
            from studybuddy.db import Database
            db = await Database.connect(pg_harness.dsn_for("sb_app"), min_size=1, max_size=1)
            try:
                async with db.scoped(self.A["id"], self.A["s1"], "student") as conn:
                    n = (await (await conn.execute("SELECT count(*) FROM conversation")).fetchone())[0]
                    self.assertEqual(n, 1)
                async with db.pool.connection() as conn:  # the SAME pooled connection, outside any scope
                    leftover = (await (await conn.execute("SELECT current_setting('app.tenant_id', true)")).fetchone())[0]
                    self.assertIn(leftover, (None, ""))
                    self.assertEqual((await (await conn.execute("SELECT count(*) FROM conversation")).fetchone())[0], 0)
            finally:
                await db.close()
        asyncio.run(go())

    def test_malformed_scope_values_are_rejected_before_reaching_sql(self):
        from studybuddy.db import Database, ScopeError

        async def go():
            db = await Database.connect(pg_harness.dsn_for("sb_app"), min_size=1, max_size=1)
            try:
                for tenant, user, role in (("", None, None), ("x'; drop table course;--", None, None), (self.A["id"], "nope", None),
                                           (self.A["id"], self.A["s1"], "superuser")):
                    with self.assertRaises(ScopeError):
                        async with db.scoped(tenant, user, role):
                            pass
            finally:
                await db.close()
        asyncio.run(go())


class UserPrivacyTests(RlsCase):
    """D4 and LLD 02: students see only their own conversations; nobody reads another user's chat directly."""

    def test_student_sees_only_own_conversation_and_messages(self):
        who = self.w.who("A", "s1")
        conv = {str(r[0]) for r in self.rows("SELECT id FROM conversation", *who)}
        self.assertEqual(conv, {self.A["conv_s1"]})
        texts = {r[0] for r in self.rows("SELECT content_redacted FROM message", *who)}
        self.assertEqual(texts, {"s1 secret question", "s1 answer"})

    def test_student_cannot_read_a_classmates_messages_by_id(self):
        who = self.w.who("A", "s1")
        self.assertEqual(self.rows("SELECT 1 FROM message WHERE conversation_id = %s", *who, params=(self.A["conv_s2"],)), [])

    def test_staff_roles_have_no_direct_access_to_message_content(self):
        for user in ("instr", "admin", "tutor"):
            self.assertEqual(self.rows("SELECT count(*) FROM message", *self.w.who("A", user))[0][0], 0, user)
            self.assertEqual(self.rows("SELECT count(*) FROM conversation", *self.w.who("A", user))[0][0], 0, user)

    def test_raw_chat_and_chunks_are_unreadable_to_the_request_path(self):
        who = self.w.who("A", "s1")
        for table in ("message_raw", "chunk"):
            with scoped("sb_app", *who) as c, self.assertRaises(errors.InsufficientPrivilege):
                c.execute(f"SELECT * FROM {table}")

    def test_student_cannot_insert_messages_into_someone_elses_conversation(self):
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO message (tenant_id, conversation_id, role, content_redacted) VALUES (app_tenant(), %s, 'user', 'x')", (self.A["conv_s2"],))

    def test_messages_are_append_only_for_everyone(self):
        with scoped("sb_app", *self.w.who("A", "s1")) as c:
            with self.assertRaises(errors.InsufficientPrivilege):
                c.execute("UPDATE message SET content_redacted = 'edited'")
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("DELETE FROM message")
        with sup() as c, self.assertRaises(errors.InsufficientPrivilege):  # even the superuser hits the trigger
            c.execute("UPDATE message SET content_redacted = 'edited'")


class CourseScopeTests(RlsCase):
    def test_enrolled_student_sees_course_and_documents_but_unenrolled_does_not(self):
        s1, s3 = self.w.who("A", "s1"), self.w.who("A", "s3")
        self.assertEqual({str(r[0]) for r in self.rows("SELECT id FROM course", *s1)}, {self.A["c1"]})
        self.assertEqual(len(self.rows("SELECT 1 FROM document", *s1)), 1)
        self.assertEqual(self.rows("SELECT 1 FROM course", *s3), [])
        self.assertEqual(self.rows("SELECT 1 FROM document", *s3), [])

    def test_cannot_start_a_conversation_in_an_unenrolled_course(self):
        with scoped("sb_app", *self.w.who("A", "s3")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO conversation (tenant_id, user_id, course_id) VALUES (app_tenant(), app_user_id(), %s)", (self.A["c1"],))
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO conversation (tenant_id, user_id, course_id) VALUES (app_tenant(), app_user_id(), %s)", (self.A["c2"],))
        with scoped("sb_app", *self.w.who("A", "s1")) as c:
            c.execute("INSERT INTO conversation (tenant_id, user_id, course_id) VALUES (app_tenant(), app_user_id(), %s)", (self.A["c1"],))

    def test_enrollment_rows_are_private_to_the_user_and_admins(self):
        self.assertEqual({str(r[0]) for r in self.rows("SELECT user_id FROM enrollment", *self.w.who("A", "s1"))}, {self.A["s1"]})
        self.assertEqual(len(self.rows("SELECT 1 FROM enrollment", *self.w.who("A", "admin"))), 4)

    def test_only_the_courses_instructor_can_add_documents(self):
        sql = "INSERT INTO document (tenant_id, course_id, title) VALUES (app_tenant(), %s, 'new')"
        with scoped("sb_app", *self.w.who("A", "instr")) as c:
            c.execute(sql, (self.A["c1"],))
        with scoped("sb_app", *self.w.who("A", "instr")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute(sql, (self.A["c2"],))  # a course they do not teach
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute(sql, (self.A["c1"],))

    def test_students_cannot_see_document_versions_or_object_keys(self):
        self.assertEqual(self.rows("SELECT 1 FROM document_version", *self.w.who("A", "s1")), [])
        self.assertEqual(len(self.rows("SELECT 1 FROM document_version", *self.w.who("A", "instr"))), 1)

    def test_document_version_content_is_immutable_but_status_can_change(self):
        with scoped("sb_worker", self.A["id"]) as c:
            c.execute("UPDATE document_version SET status = 'failed', error_reason = 'x'")
        for column, value in (("object_key", "'other'"), ("sha256", f"'{'b' * 64}'"), ("version_no", "9")):
            with scoped("sb_worker", self.A["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
                c.execute(f"UPDATE document_version SET {column} = {value}")


class RoleManagementTests(RlsCase):
    def test_a_student_cannot_promote_themselves_or_anyone(self):
        with scoped("sb_app", *self.w.who("A", "s1")) as c:
            self.assertEqual(c.execute("UPDATE app_user SET role = 'tenant_admin' WHERE id = app_user_id()").rowcount, 0)
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE app_user SET idp_subject = 'someone-else' WHERE id = app_user_id()")  # no column grant

    def test_a_tenant_admin_can_manage_users_in_their_own_tenant_only(self):
        with scoped("sb_app", *self.w.who("A", "admin")) as c:
            self.assertEqual(c.execute("UPDATE app_user SET status = 'disabled' WHERE id = %s", (self.A["s3"],)).rowcount, 1)
            self.assertEqual(c.execute("UPDATE app_user SET status = 'disabled' WHERE id = %s", (self.B["s3"],)).rowcount, 0)
            self.assertEqual(len(c.execute("SELECT 1 FROM app_user").fetchall()), 6)

    def test_users_see_only_themselves_staff_see_nobody_else(self):
        self.assertEqual(len(self.rows("SELECT 1 FROM app_user", *self.w.who("A", "s1"))), 1)
        self.assertEqual(len(self.rows("SELECT 1 FROM app_user", *self.w.who("A", "tutor"))), 1)

    def test_authentication_role_can_provision_but_only_inside_the_verified_tenant(self):
        with scoped("sb_auth", self.A["id"]) as c:
            c.execute("INSERT INTO app_user (tenant_id, idp_subject, role) VALUES (app_tenant(), 'new-sub', 'student')")
            self.assertEqual(c.execute("SELECT count(*) FROM app_user WHERE idp_subject = 'new-sub'").fetchone()[0], 1)
        with scoped("sb_auth", self.A["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO app_user (tenant_id, idp_subject, role) VALUES (%s, 'sneaky', 'tenant_admin')", (self.B["id"],))
        with scoped("sb_auth", self.A["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE app_user SET role = 'tenant_admin'")  # sb_auth cannot update at all


class TutorAuditedReadTests(RlsCase):
    def read(self, who, ticket, raw=False):
        with scoped("sb_app", *who) as c:
            rows = c.execute("SELECT * FROM tutor_read_ticket(%s, %s)", (ticket, raw)).fetchall()
            audit = c.execute("SELECT action, target, actor_id FROM audit_log WHERE tenant_id = app_tenant() AND action <> 'seed'").fetchall() \
                if who[2] == "tenant_admin" else None
            c.commit()  # keep the audit row for the next assertion (scoped() rolls back what is left)
        return rows

    def audit_rows(self):
        with sup() as c:
            return c.execute("SELECT action, target, actor_id::text, tenant_id::text FROM audit_log WHERE action <> 'seed' ORDER BY at").fetchall()

    def test_assigned_tutor_reads_the_conversation_and_the_read_is_audited(self):
        rows = self.read(self.w.who("A", "tutor"), self.A["ticket"])
        self.assertEqual([r[2] for r in rows], ["s1 secret question", "s1 answer"])
        self.assertTrue(all(r[3] is None for r in rows))  # no raw text unless asked for
        self.assertEqual(self.audit_rows(), [("chat.read", f"ticket:{self.A['ticket']}", self.A["tutor"], self.A["id"])])

    def test_raw_read_is_audited_as_a_raw_read_and_returns_raw_text(self):
        rows = self.read(self.w.who("A", "tutor"), self.A["ticket"], raw=True)
        self.assertIn("s1 RAW text", [r[3] for r in rows])
        self.assertEqual(self.audit_rows()[0][0], "raw_chat.read")

    def test_unassigned_or_foreign_tickets_are_refused_and_not_audited(self):
        with sup() as c:  # a second tutor in tenant A, and a ticket for s2 assigned to nobody
            other = new()
            c.execute("INSERT INTO app_user (id, tenant_id, idp_subject, role) VALUES (%s,%s,'t2','tutor')", (other, self.A["id"]))
            tid = new()
            c.execute("INSERT INTO escalation_ticket (id, tenant_id, course_id, conversation_id, message_id, reason) VALUES (%s,%s,%s,%s,%s,'q')",
                      (tid, self.A["id"], self.A["c1"], self.A["conv_s2"], self.A["msg_s2_assistant"]))
        for who, ticket in ((self.w.who("A", "tutor"), tid),                       # not assigned to this tutor
                            ((self.A["id"], other, "tutor"), self.A["ticket"]),    # assigned to a different tutor
                            (self.w.who("B", "tutor"), self.A["ticket"]),          # another tenant's tutor
                            (self.w.who("A", "tutor"), new())):                    # no such ticket
            with scoped("sb_app", *who) as c, self.assertRaises(errors.InsufficientPrivilege):
                c.execute("SELECT * FROM tutor_read_ticket(%s)", (ticket,))
        self.assertEqual(self.audit_rows(), [])

    def test_only_tutors_may_call_it(self):
        for user in ("admin", "instr", "s1"):
            with scoped("sb_app", *self.w.who("A", user)) as c, self.assertRaises(errors.InsufficientPrivilege):
                c.execute("SELECT * FROM tutor_read_ticket(%s)", (self.A["ticket"],))

    def test_the_function_cannot_be_used_without_a_tenant_scope_or_by_other_roles(self):
        with scoped("sb_app") as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("SELECT * FROM tutor_read_ticket(%s)", (self.A["ticket"],))
        for role in ("sb_worker", "sb_eval", "sb_auth"):
            with scoped(role, *self.w.who("A", "tutor")) as c, self.assertRaises(errors.InsufficientPrivilege):
                c.execute("SELECT * FROM tutor_read_ticket(%s)", (self.A["ticket"],))


class TicketTests(RlsCase):
    def test_visibility(self):
        self.assertEqual(len(self.rows("SELECT 1 FROM escalation_ticket", *self.w.who("A", "tutor"))), 1)
        self.assertEqual(len(self.rows("SELECT 1 FROM escalation_ticket", *self.w.who("A", "admin"))), 1)
        self.assertEqual(len(self.rows("SELECT 1 FROM escalation_ticket", *self.w.who("A", "s1"))), 1)   # the asker
        self.assertEqual(self.rows("SELECT 1 FROM escalation_ticket", *self.w.who("A", "s2")), [])       # a classmate
        self.assertEqual(self.rows("SELECT 1 FROM escalation_ticket", *self.w.who("A", "instr")), [])

    def test_a_tutor_cannot_reassign_tickets_but_an_admin_can(self):
        with scoped("sb_app", *self.w.who("A", "tutor")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE escalation_ticket SET assigned_tutor_id = %s", (self.A["instr"],))
        with scoped("sb_app", *self.w.who("A", "admin")) as c:
            self.assertEqual(c.execute("UPDATE escalation_ticket SET assigned_tutor_id = %s", (self.A["instr"],)).rowcount, 1)

    def test_tutor_can_resolve_their_ticket_but_not_someone_elses(self):
        with scoped("sb_app", *self.w.who("A", "tutor")) as c:
            self.assertEqual(c.execute("UPDATE escalation_ticket SET status = 'resolved', resolution = 'done'").rowcount, 1)
        with sup() as c:
            c.execute("SET session_replication_role = replica")  # fixture change only: skip the assignment guard trigger
            c.execute("UPDATE escalation_ticket SET assigned_tutor_id = NULL")
        with scoped("sb_app", *self.w.who("A", "tutor")) as c:
            self.assertEqual(c.execute("UPDATE escalation_ticket SET status = 'resolved'").rowcount, 0)

    def test_a_student_can_open_a_ticket_only_on_their_own_conversation(self):
        sql = ("INSERT INTO escalation_ticket (tenant_id, course_id, conversation_id, message_id, reason, student_id) "
               "VALUES (app_tenant(), %s, %s, %s, 'help', app_user_id())")
        with scoped("sb_app", *self.w.who("A", "s2")) as c:
            c.execute(sql, (self.A["c1"], self.A["conv_s2"], self.A["msg_s2_assistant"]))
        with scoped("sb_app", *self.w.who("A", "s2")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute(sql, (self.A["c1"], self.A["conv_s1"], self.A["msg_s1_assistant"]))


class RawChatRetentionTests(RlsCase):
    INSERT = "INSERT INTO message_raw (tenant_id, message_id, content) VALUES (app_tenant(), %s, 'raw')"

    def test_raw_chat_is_stored_only_for_tenants_that_opted_in(self):
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute(self.INSERT, (self.A["msg_s1_user"],))  # tenant A: eval_retention = false
        with scoped("sb_app", *self.w.who("B", "s1")) as c:
            c.execute(self.INSERT, (self.B["msg_s1_user"],))  # tenant B: opted in

    def test_only_the_evaluation_role_reads_raw_chat_and_only_for_opted_in_tenants(self):
        self.assertEqual(self.rows("SELECT count(*) FROM message_raw", self.A["id"], db_role="sb_eval")[0][0], 0)  # seeded, but A opted out
        self.assertEqual(self.rows("SELECT count(*) FROM message_raw", self.B["id"], db_role="sb_eval")[0][0], 2)
        with scoped("sb_eval", self.B["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("SELECT content_redacted FROM message")  # nothing but message_raw

    def test_the_purge_job_deletes_expired_raw_chat_without_being_able_to_read_chat(self):
        with sup() as c:
            c.execute("UPDATE message_raw SET expires_at = now() - interval '1 day' WHERE tenant_id = %s", (self.B["id"],))
        with scoped("sb_worker", self.B["id"]) as c:
            self.assertEqual(c.execute("DELETE FROM message_raw WHERE expires_at < now()").rowcount, 2)
        with scoped("sb_worker", self.B["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("SELECT content FROM message_raw")
        with scoped("sb_worker", self.B["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("SELECT content_redacted FROM message")


class AuditLogTests(RlsCase):
    def test_audit_log_is_append_only_for_everyone(self):
        with scoped("sb_app", *self.w.who("A", "admin")) as c:
            c.execute("INSERT INTO audit_log (tenant_id, actor_id, action) VALUES (app_tenant(), app_user_id(), 'role.change')")
        for sql in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
            with scoped("sb_app", *self.w.who("A", "admin")) as c, self.assertRaises(errors.InsufficientPrivilege):
                c.execute(sql)
            with sup() as c, self.assertRaises(errors.InsufficientPrivilege):  # the trigger stops even a superuser
                c.execute(sql)

    def test_only_a_tenant_admin_reads_the_audit_log_and_only_their_own_tenant(self):
        self.assertEqual(len(self.rows("SELECT 1 FROM audit_log", *self.w.who("A", "admin"))), 1)
        for user in ("s1", "instr", "tutor"):
            self.assertEqual(self.rows("SELECT 1 FROM audit_log", *self.w.who("A", user)), [], user)

    def test_cannot_write_an_audit_row_for_another_tenant(self):
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO audit_log (tenant_id, action) VALUES (%s, 'forged')", (self.B["id"],))


class UsageAndOutboxTests(RlsCase):
    def test_usage_ledger_rows_are_per_user(self):
        self.assertEqual(len(self.rows("SELECT 1 FROM usage_ledger", *self.w.who("A", "s1"))), 1)
        self.assertEqual(self.rows("SELECT 1 FROM usage_ledger", *self.w.who("A", "s2")), [])
        with scoped("sb_app", *self.w.who("A", "s2")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO usage_ledger (tenant_id, user_id, day) VALUES (app_tenant(), %s, current_date)", (self.A["s1"],))

    def test_outbox_is_write_only_for_requests_and_readable_by_the_worker_in_its_tenant(self):
        with scoped("sb_app", *self.w.who("A", "s1")) as c:
            c.execute("INSERT INTO outbox_event (tenant_id, type, payload) VALUES (app_tenant(), 'document.changed', '{}')")
        with scoped("sb_app", *self.w.who("A", "s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("SELECT * FROM outbox_event")
        self.assertEqual(self.rows("SELECT count(*) FROM outbox_event", self.A["id"], db_role="sb_worker")[0][0], 1)
        with scoped("sb_worker", self.A["id"]) as c:
            self.assertEqual(c.execute("UPDATE outbox_event SET published_at = now()").rowcount, 1)
        with scoped("sb_worker", self.A["id"]) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE outbox_event SET payload = '{\"x\":1}'")


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def test_rerunning_is_a_no_op_and_editing_an_applied_file_is_refused(self):
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock
        from studybuddy.db import MigrationError, migrate
        import importlib
        migrate_module = importlib.import_module("studybuddy.db.migrate")

        with psycopg.connect(pg_harness.dsn_for("sb_owner")) as conn:
            self.assertEqual(migrate(conn), [])
            tmp = Path(tempfile.mkdtemp())
            try:
                for f in migrate_module.SQL_DIR.glob("*.sql"):
                    shutil.copy(f, tmp / f.name)
                (tmp / "001_schema.sql").write_text((tmp / "001_schema.sql").read_text() + "\n-- edited\n")
                with mock.patch.object(migrate_module, "SQL_DIR", tmp), self.assertRaises(MigrationError):
                    migrate(conn)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
