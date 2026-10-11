"""Ticket queue, claim, student actions, replies and the bounded tutor window, against real PostgreSQL (LLD 10 s3, s4, s9)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL and psycopg are required")

from psycopg import errors

from tests.test_rls import RlsCase, new, scoped, sup


class TicketRlsCase(RlsCase):
    def setUp(self):
        super().setUp()
        A = self.A
        with sup() as c:
            A["tutor2"], A["tutor_other_course"] = new(), new()
            for key, enroll in (("tutor2", True), ("tutor_other_course", False)):
                c.execute("INSERT INTO app_user (id, tenant_id, idp_subject, role) VALUES (%s,%s,%s,'tutor')", (A[key], A["id"], f"sub-{key}"))
            c.execute("INSERT INTO enrollment VALUES (%s,%s,%s,'tutor')", (A["id"], A["c1"], A["tutor2"]))
            c.execute("INSERT INTO enrollment VALUES (%s,%s,%s,'tutor')", (A["id"], A["c2"], A["tutor_other_course"]))
            A["open"], A["restricted"] = new(), new()
            for key, restricted in (("open", False), ("restricted", True)):
                c.execute("INSERT INTO escalation_ticket (id, tenant_id, course_id, conversation_id, message_id, reason, student_id, restricted, priority) "
                          "VALUES (%s,%s,%s,%s,%s,'grade dispute',%s,%s,'high')",
                          (A[key], A["id"], A["c1"], A["conv_s2"], A["msg_s2_user"], A["s2"], restricted))

    def ident(self, key, tenant="A"):
        t, u, r = self.w.who(tenant, key)
        return t, u, "tutor" if key.startswith("tutor") else r

    def ticket_ids(self, who, tenant="A"):
        return {str(r[0]) for r in self.rows("SELECT id FROM escalation_ticket", *self.ident(who, tenant))}


class QueueTests(TicketRlsCase):
    def test_tutors_see_the_unclaimed_queue_of_their_own_course_only(self):
        self.assertIn(self.A["open"], self.ticket_ids("tutor2"))
        self.assertNotIn(self.A["open"], self.ticket_ids("tutor_other_course"))                      # not on this course's roster
        self.assertEqual(self.ticket_ids("tutor"), {self.A["ticket"], self.A["open"]})               # own assigned ticket + the queue
        self.assertEqual(self.ticket_ids("s1"), {self.A["ticket"]})                                  # a student sees only their own
        self.assertEqual(self.ticket_ids("s2"), {self.A["open"], self.A["restricted"]})
        self.assertEqual(self.rows("SELECT count(*) FROM escalation_ticket", *self.ident("admin", "B"))[0][0], 1)  # other tenant: only its own

    def test_restricted_tickets_need_a_designated_tutor(self):
        self.assertNotIn(self.A["restricted"], self.ticket_ids("tutor2"))
        with sup() as c:
            c.execute("UPDATE enrollment SET sensitive_ok = true WHERE user_id = %s", (self.A["tutor2"],))
        self.assertIn(self.A["restricted"], self.ticket_ids("tutor2"))

    def test_the_queue_does_not_open_the_conversation(self):
        with scoped("sb_app", *self.ident("tutor2")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("SELECT * FROM tutor_read_ticket(%s)", (self.A["open"],))
        self.assertEqual(self.rows("SELECT count(*) FROM message", *self.ident("tutor2"))[0][0], 0)

    def test_instructor_sees_tickets_only_after_they_are_escalated_past_tutors(self):
        self.assertEqual(self.ticket_ids("instr"), set())
        with sup() as c:
            c.execute("UPDATE escalation_ticket SET notify_level = 1 WHERE id = %s", (self.A["open"],))
        self.assertEqual(self.ticket_ids("instr"), {self.A["open"]})


class ClaimTests(TicketRlsCase):
    CLAIM = "UPDATE escalation_ticket SET assigned_tutor_id = app_user_id(), status = 'assigned', claimed_at = now() WHERE id = %s"

    def claim(self, who, ticket):
        with scoped("sb_app", *self.ident(who)) as c:
            return c.execute(self.CLAIM, (ticket,)).rowcount

    def test_first_claim_wins_and_the_second_updates_nothing(self):
        self.assertEqual(self.claim("tutor2", self.A["open"]), 1)
        with sup() as c:
            c.execute("SET session_replication_role = replica")
            c.execute("UPDATE escalation_ticket SET assigned_tutor_id = %s WHERE id = %s", (self.A["tutor2"], self.A["open"]))
        self.assertEqual(self.claim("tutor", self.A["open"]), 0)                                      # already taken

    def test_cannot_claim_restricted_unrostered_assigned_or_foreign_tickets(self):
        self.assertEqual(self.claim("tutor2", self.A["restricted"]), 0)
        self.assertEqual(self.claim("tutor_other_course", self.A["open"]), 0)
        self.assertEqual(self.claim("tutor2", self.A["ticket"]), 0)                                   # assigned to someone else
        with scoped("sb_app", *self.ident("tutor", "B")) as c:
            self.assertEqual(c.execute(self.CLAIM, (self.A["open"],)).rowcount, 0)                    # other tenant

    def test_a_tutor_cannot_hand_a_ticket_to_someone_else_or_change_priority(self):
        with scoped("sb_app", *self.ident("tutor2")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE escalation_ticket SET assigned_tutor_id = %s, status = 'assigned' WHERE id = %s", (self.A["tutor"], self.A["open"]))
        with scoped("sb_app", *self.ident("tutor")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE escalation_ticket SET priority = 'low' WHERE id = %s", (self.A["ticket"],))
        with scoped("sb_app", *self.ident("tutor")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE escalation_ticket SET status = 'open' WHERE id = %s", (self.A["ticket"],))
        with scoped("sb_app", *self.ident("tutor")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE escalation_ticket SET reason = 'edited' WHERE id = %s", (self.A["ticket"],))

    def test_assigned_tutor_works_the_ticket_and_notification_state_is_system_only(self):
        with scoped("sb_app", *self.ident("tutor")) as c:
            n = c.execute("UPDATE escalation_ticket SET status = 'in_progress', first_response_at = now() WHERE id = %s", (self.A["ticket"],)).rowcount
            self.assertEqual(n, 1)
        with scoped("sb_app", *self.ident("tutor")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("UPDATE escalation_ticket SET notify_level = 2 WHERE id = %s", (self.A["ticket"],))


class StudentActionTests(TicketRlsCase):
    def update(self, who, sql, params=()):
        with scoped("sb_app", *self.ident(who)) as c:
            return c.execute(sql, params).rowcount

    def test_student_can_cancel_and_reopen_only_their_own_ticket(self):
        self.assertEqual(self.update("s2", "UPDATE escalation_ticket SET status = 'closed' WHERE id = %s", (self.A["open"],)), 1)
        self.assertEqual(self.update("s1", "UPDATE escalation_ticket SET status = 'closed' WHERE id = %s", (self.A["open"],)), 0)  # someone else's
        with sup() as c:
            c.execute("UPDATE escalation_ticket SET status = 'resolved', resolution = 'ok' WHERE id = %s", (self.A["ticket"],))
        self.assertEqual(self.update("s1", "UPDATE escalation_ticket SET status = 'reopened' WHERE id = %s", (self.A["ticket"],)), 1)

    def test_student_cannot_touch_anything_else(self):
        for sql in ("UPDATE escalation_ticket SET priority = 'urgent', status = 'closed'", "UPDATE escalation_ticket SET resolution = 'fixed'",
                    "UPDATE escalation_ticket SET assigned_tutor_id = NULL", "UPDATE escalation_ticket SET approved_faq = true",
                    "UPDATE escalation_ticket SET status = 'resolved'", "UPDATE escalation_ticket SET status = 'reopened'",  # not resolved yet
                    "UPDATE escalation_ticket SET reason = 'x'"):
            with scoped("sb_app", *self.ident("s2")) as c, self.assertRaises(errors.InsufficientPrivilege, msg=sql):
                c.execute(sql + " WHERE id = %s", (self.A["open"],))

    def test_a_student_cannot_open_a_ticket_for_someone_else(self):
        with scoped("sb_app", *self.ident("s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO escalation_ticket (tenant_id, course_id, conversation_id, message_id, reason, student_id) "
                      "VALUES (app_tenant(), %s, %s, %s, 'x', %s)", (self.A["c1"], self.A["conv_s1"], self.A["msg_s1_user"], self.A["s2"]))


class ReplyTests(TicketRlsCase):
    INSERT = "INSERT INTO ticket_reply (tenant_id, ticket_id, author_id, author_role, body) VALUES (app_tenant(), %s, app_user_id(), %s, 'hello')"

    def test_student_and_assigned_tutor_converse_and_nobody_else_sees_it(self):
        with scoped("sb_app", *self.ident("s1")) as c:
            c.execute(self.INSERT, (self.A["ticket"], "student"))
            c.commit()
        with scoped("sb_app", *self.ident("tutor")) as c:
            c.execute(self.INSERT, (self.A["ticket"], "tutor"))
            c.commit()
        for who, expected in (("s1", 2), ("tutor", 2), ("admin", 2), ("s2", 0), ("tutor2", 0), ("instr", 0), ("tutor", 2)):
            self.assertEqual(self.rows("SELECT count(*) FROM ticket_reply", *self.ident(who))[0][0], expected, who)
        self.assertEqual(self.rows("SELECT count(*) FROM ticket_reply", *self.ident("admin", "B"))[0][0], 0)

    def test_wrong_author_wrong_role_or_closed_ticket_is_refused(self):
        cases = [("s2", self.A["ticket"], "student"), ("tutor2", self.A["ticket"], "tutor"), ("s1", self.A["ticket"], "tutor"),
                 ("tutor", self.A["ticket"], "student"), ("admin", self.A["ticket"], "tutor")]
        for who, ticket, role in cases:
            with scoped("sb_app", *self.ident(who)) as c, self.assertRaises(errors.InsufficientPrivilege, msg=who + role):
                c.execute(self.INSERT, (ticket, role))
        with sup() as c:
            c.execute("UPDATE escalation_ticket SET status = 'closed' WHERE id = %s", (self.A["ticket"],))
        with scoped("sb_app", *self.ident("s1")) as c, self.assertRaises(errors.InsufficientPrivilege):
            c.execute(self.INSERT, (self.A["ticket"], "student"))

    def test_replies_are_append_only(self):
        with sup() as c:
            c.execute("INSERT INTO ticket_reply (tenant_id, ticket_id, author_id, author_role, body) VALUES (%s,%s,%s,'tutor','x')",
                      (self.A["id"], self.A["ticket"], self.A["tutor"]))
        for sql in ("UPDATE ticket_reply SET body = 'changed'", "DELETE FROM ticket_reply"):
            with scoped("sb_app", *self.ident("tutor")) as c, self.assertRaises((errors.InsufficientPrivilege, errors.RaiseException)):
                c.execute(sql)


class TutorWindowTests(TicketRlsCase):
    def test_window_is_the_escalated_message_ten_before_and_the_bot_answer_after(self):
        A = self.A
        with sup() as c:
            c.execute("TRUNCATE message_raw")
            c.execute("DELETE FROM escalation_ticket WHERE id = %s", (A["ticket"],))
            ids = []
            for i in range(30):
                role = "user" if i % 2 == 0 else "assistant"
                mid = new()
                c.execute("INSERT INTO message (id, tenant_id, conversation_id, role, content_redacted, created_at) "
                          "VALUES (%s,%s,%s,%s,%s, now() + make_interval(secs => %s))", (mid, A["id"], A["conv_s1"], role, f"m{i}", i + 100))
                ids.append(mid)
            t = new()
            c.execute("INSERT INTO escalation_ticket (id, tenant_id, course_id, conversation_id, message_id, reason, student_id, status, assigned_tutor_id) "
                      "VALUES (%s,%s,%s,%s,%s,'grade',%s,'assigned',%s)", (t, A["id"], A["c1"], A["conv_s1"], ids[20], A["s1"], A["tutor"]))
        with scoped("sb_app", *self.ident("tutor")) as c:
            texts = [r[2] for r in c.execute("SELECT * FROM tutor_read_ticket(%s)", (t,)).fetchall()]
        self.assertEqual(texts[-1], "m21")                       # the bot's answer right after the escalated message m20
        self.assertIn("m20", texts)
        self.assertNotIn("m22", texts)                           # nothing later
        self.assertNotIn("m5", texts)                            # nothing from long before
        self.assertLessEqual(len(texts), 12)


if __name__ == "__main__":
    unittest.main()
