"""Escalation end to end against real PostgreSQL (LLD 10 s9): triggers, dedup, access, window, SLA, notifications, FAQ."""
import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import pg_harness

if not pg_harness.available():
    raise unittest.SkipTest("PostgreSQL and psycopg are required")

from studybuddy.cache import InMemoryCacheStore, SemanticCache
from studybuddy.db import ConversationRepo, Database
from studybuddy.escalation import (EscalationConfig, EscalationUnavailable, FaqService, FaqSync, InMemoryChannel, NotificationFailed, Notifier,
                                   PgEscalation, RateLimited, SlaMonitor, TicketAccessDenied, TicketService, plan_for)
from studybuddy.guardrails import RuleInputGuardrails, RuleOutputGuardrails
from studybuddy.ingestion import InMemoryBroker, OutboxRelay
from studybuddy.pipeline import Identity, Orchestrator, PipelineConfig, RequestContext, fakes
from studybuddy.retrieval import HybridRetriever, InMemoryChunkStore
from studybuddy.retrieval.fakes import HashingEmbedder
from studybuddy.router import Router
from tests.test_rls import World, new, sup

SAN = RuleInputGuardrails().sanitize


class Versions:
    def __init__(self, **docs):
        self.docs = docs

    async def current_version(self, tenant_id, course_id, document_id):
        return self.docs.get(document_id)


class Rig(unittest.TestCase):
    cfg = EscalationConfig()

    @classmethod
    def setUpClass(cls):
        pg_harness.start()

    def setUp(self):
        self.w = World()
        self.A, self.B = self.w.t["A"], self.w.t["B"]
        A = self.A
        with sup() as c:
            A["tutor2"], A["tutor3"] = new(), new()
            for key in ("tutor2", "tutor3"):
                c.execute("INSERT INTO app_user (id, tenant_id, idp_subject, role) VALUES (%s,%s,%s,'tutor')", (A[key], A["id"], f"sub-{key}"))
            c.execute("INSERT INTO enrollment VALUES (%s,%s,%s,'tutor')", (A["id"], A["c1"], A["tutor2"]))
            c.execute("INSERT INTO enrollment (tenant_id, course_id, user_id, role_in_course, sensitive_ok) VALUES (%s,%s,%s,'tutor',true)", (A["id"], A["c1"], A["tutor3"]))
            c.execute("DELETE FROM escalation_ticket")
            A["conv_c2"] = new()
            c.execute("INSERT INTO enrollment VALUES (%s,%s,%s,'student')", (A["id"], A["c2"], A["s2"]))
            c.execute("INSERT INTO conversation (id, tenant_id, user_id, course_id) VALUES (%s,%s,%s,%s)", (A["conv_c2"], A["id"], A["s2"], A["c2"]))
            A["msg_c2"] = new()
            c.execute("INSERT INTO message (id, tenant_id, conversation_id, role, content_redacted) VALUES (%s,%s,%s,'user','c2 question')", (A["msg_c2"], A["id"], A["conv_c2"]))

    def who(self, key, tenant="A"):
        t = self.w.t[tenant]
        role = "tutor" if key.startswith("tutor") else {"admin": "tenant_admin", "instr": "instructor"}.get(key, "student")
        return Identity(t["id"], t[key], role)

    def go(self, fn, cfg=None):
        async def main():
            dbs = {r: await Database.connect(pg_harness.dsn_for(r), max_size=4) for r in ("sb_app", "sb_worker", "sb_relay")}
            try:
                esc = PgEscalation(dbs["sb_app"], cfg or self.cfg)
                svc = TicketService(dbs["sb_app"], esc, SAN)
                return await fn(dbs, esc, svc)
            finally:
                for d in dbs.values():
                    await d.close()
        return asyncio.run(main())

    def ctx(self, key="s2", question="I want to dispute my grade", conversation="conv_s2", course="c1"):
        return RequestContext(self.who(key), self.A[course], question, f"req-{uuid.uuid4().hex[:8]}", conversation_id=self.A[conversation])

    def one(self, sql, params=()):
        with sup() as c:
            return c.execute(sql, params).fetchall()

    def tickets(self):
        return self.one("SELECT priority, trigger, restricted, status FROM escalation_ticket ORDER BY created_at")


class TriggerTests(Rig):
    def test_each_trigger_makes_exactly_one_ticket_with_the_right_priority(self):
        cases = {"rule:safety.2": ("urgent", "safety", True), "rule:escalate.4": ("high", "sensitive_topic", False),
                 "rule:escalate.1": ("high", "sensitive_topic", True), "classifier:sensitive": ("normal", "sensitive_topic", False),
                 "low_confidence": ("normal", "low_confidence", False), "degraded": ("normal", "degraded", False),
                 "something unknown": ("normal", "router", False)}
        for reason, (prio, trig, restricted) in cases.items():
            self.assertEqual((plan_for(reason).priority, plan_for(reason).trigger.value, plan_for(reason).restricted), (prio, trig, restricted), reason)

        async def run(dbs, esc, svc):
            for reason in ("rule:escalate.4", "classifier:sensitive", "rule:safety.2"):
                await esc.create(self.ctx(), reason)
            await svc.request_tutor(self.who("s1"), self.A["c1"], self.A["conv_s1"], "please help")
        self.go(run)
        self.assertEqual(sorted(self.tickets()), sorted([("high", "sensitive_topic", False, "open"), ("normal", "sensitive_topic", False, "open"),
                                                         ("urgent", "safety", True, "open"), ("normal", "user_request", False, "open")]))
        self.assertEqual(self.one("SELECT count(*) FROM outbox_event WHERE type = 'escalation.created'")[0][0], 4)
        self.assertEqual(self.one("SELECT count(*) FROM audit_log WHERE action = 'ticket.open'")[0][0], 4)
        self.assertTrue(self.one("SELECT sla_deadline - created_at FROM escalation_ticket WHERE priority = 'urgent'")[0][0].total_seconds() == 3600)

    def test_question_is_stored_once_and_ticket_points_at_it(self):
        async def run(dbs, esc, svc):
            ctx = self.ctx(question="Dispute my grade please")
            tid = await esc.create(ctx, "rule:escalate.4")
            self.assertIsNotNone(ctx.user_message_id)
            ctx.answer, ctx.route = "I've passed this to your tutor.", "human"
            await ConversationRepo(dbs["sb_app"]).record_turn(ctx)
            return tid, ctx.user_message_id
        tid, mid = self.go(run)
        self.assertEqual(self.one("SELECT count(*) FROM message WHERE content_redacted = 'Dispute my grade please'")[0][0], 1)
        self.assertEqual(str(self.one("SELECT message_id FROM escalation_ticket WHERE id = %s", (tid,))[0][0]), mid)

    def test_no_conversation_means_no_ticket_not_a_wrong_one(self):
        async def run(dbs, esc, svc):
            ctx = self.ctx()
            ctx2 = RequestContext(ctx.identity, ctx.course_id, "q", "req-12345678")
            with self.assertRaises(EscalationUnavailable):
                await esc.create(ctx2, "rule:escalate.4")
        self.go(run)
        self.assertEqual(self.tickets(), [])


class DedupAndRateTests(Rig):
    def test_repeat_attaches_to_the_open_ticket_and_safety_never_duplicates(self):
        async def run(dbs, esc, svc):
            a = await esc.create(self.ctx(), "rule:escalate.4")
            b = await esc.create(self.ctx(), "rule:escalate.4")
            c = await esc.create(self.ctx(), "low_confidence")            # a different topic is a new ticket
            s1 = await esc.create(self.ctx(), "rule:safety.1")
            s2 = await esc.create(self.ctx(), "rule:safety.5")            # another safety phrase: still the one urgent ticket
            return a, b, c, s1, s2
        a, b, c, s1, s2 = self.go(run)
        self.assertEqual(a, b)
        self.assertEqual(s1, s2)
        self.assertEqual(len({a, c, s1}), 3)
        self.assertEqual(len(self.tickets()), 3)

    def test_identical_requests_in_parallel_make_one_ticket(self):
        async def run(dbs, esc, svc):
            return await asyncio.gather(*(esc.create(self.ctx(), "rule:escalate.4") for _ in range(6)))
        ids = self.go(run)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.tickets()), 1)

    def test_rate_limit_per_student(self):
        cfg = EscalationConfig(rate_limit_per_hour=2)

        async def run(dbs, esc, svc):
            first = await esc.create(self.ctx(), "rule:escalate.4")
            second = await esc.create(self.ctx(), "low_confidence")
            third = await esc.create(self.ctx(), "degraded")             # over the limit: attached to the latest open ticket
            self.assertEqual(third, second)
            with sup() as c:
                c.execute("UPDATE escalation_ticket SET status = 'resolved'")
            with self.assertRaises(EscalationUnavailable):                # nothing open to attach to
                await esc.create(self.ctx(), "degraded")
            urgent = await esc.create(self.ctx(), "rule:safety.1")         # safety is never rate limited
            self.assertNotIn(urgent, (first, second))
            await esc.create(self.ctx("s1", conversation="conv_s1"), "rule:escalate.4")   # other students are unaffected
        self.go(run, cfg)
        self.assertEqual(len(self.tickets()), 4)


class PipelineIntegrationTests(Rig):
    def orch(self, esc, repo=None, **cfg):
        emb = HashingEmbedder()
        return Orchestrator(guardrails=RuleInputGuardrails(), cache=fakes.FakeCache(), router=Router(), retriever=HybridRetriever(InMemoryChunkStore(), emb),
                            generator=fakes.FakeGenerator(), output_guardrails=RuleOutputGuardrails(), escalation=esc,
                            config=PipelineConfig(**cfg), on_complete=repo.record_turn if repo else None)

    async def ask(self, orch, repo, key, question):
        who = self.who(key)
        state = await repo.load(who, self.A["c1"])
        ctx = RequestContext(who, self.A["c1"], question, f"req-{uuid.uuid4().hex[:8]}", conversation_id=state.conversation_id, history=state.history)
        events = [e async for e in orch.handle(ctx)]
        await asyncio.sleep(0.05)
        return ctx, events

    def test_sensitive_question_gets_an_immediate_message_and_a_ticket_even_if_the_relay_never_runs(self):
        async def run(dbs, esc, svc):
            repo = ConversationRepo(dbs["sb_app"])
            ctx, events = await self.ask(self.orch(esc, repo), repo, "s2", "I want to dispute my grade")
            self.assertEqual(ctx.outcome, "escalated")
            self.assertTrue(any(e.type == "ticket" for e in events))
            self.assertIn("passed this to your tutor", "".join(e.data.get("text", "") for e in events))
        self.go(run)
        self.assertEqual(self.one("SELECT count(*) FROM outbox_event WHERE type = 'escalation.created' AND published_at IS NULL")[0][0], 1)  # waiting for the relay
        self.assertEqual(self.one("SELECT count(*) FROM message WHERE content_redacted LIKE '%%dispute my grade%%'")[0][0], 1)

    def test_safety_message_and_resources_are_shown_even_if_the_ticket_cannot_be_saved(self):
        class Broken:
            async def create(self, ctx, reason):
                raise ConnectionError("postgres down")

        async def run(dbs, esc, svc):
            ctx, events = await self.ask(self.orch(Broken()), ConversationRepo(dbs["sb_app"]), "s2", "I want to kill myself")
            text = "".join(e.data.get("text", "") for e in events)
            self.assertIn("emergency", text)
            self.assertFalse(any(e.type == "error" for e in events))
            self.assertTrue(any(e.type == "notice" and e.data["code"] == "ticket_unavailable" for e in events))
            self.assertTrue(ctx.router_trace.get("ticket_failed"))
            _, events2 = await self.ask(self.orch(Broken()), ConversationRepo(dbs["sb_app"]), "s1", "I want to dispute my grade")
            self.assertTrue(any(e.type == "error" for e in events2))           # a non-safety escalation still fails honestly
        self.go(run)

    def test_second_miss_in_a_row_hands_over_to_a_tutor(self):
        async def run(dbs, esc, svc):
            repo = ConversationRepo(dbs["sb_app"])
            orch = self.orch(esc, repo)
            c1, e1 = await self.ask(orch, repo, "s2", "What is the mitochondrial isoform theorem?")
            self.assertEqual((c1.outcome, any(e.type == "ticket" for e in e1)), ("none_found", False))
            c2, e2 = await self.ask(orch, repo, "s2", "Tell me about the mitochondrial isoform theorem again")
            self.assertEqual((c2.outcome, any(e.type == "ticket" for e in e2)), ("escalated", True))
            self.assertIn("passed it to your tutor", "".join(e.data.get("text", "") for e in e2))
        self.go(run)
        self.assertEqual([t[1] for t in self.tickets()], ["low_confidence"])


class TutorFlowTests(Rig):
    def test_full_lifecycle_access_window_and_audit(self):
        async def run(dbs, esc, svc):
            ticket = await esc.create(self.ctx(question="Please re-mark my exam"), "rule:escalate.4")
            queue = await svc.queue(self.who("tutor2"))
            self.assertEqual([q["ticket_id"] for q in queue], [ticket])
            self.assertEqual(await svc.queue(self.who("tutor", "B")), [])                       # other tenant
            with self.assertRaises(TicketAccessDenied):
                await svc.read_conversation(self.who("tutor2"), ticket)                          # not claimed yet: no conversation
            self.assertTrue(await svc.claim(self.who("tutor2"), ticket))
            self.assertFalse(await svc.claim(self.who("tutor"), ticket))                         # first claim wins
            msgs = await svc.read_conversation(self.who("tutor2"), ticket)
            self.assertIn("Please re-mark my exam", [m.content_redacted for m in msgs])
            with self.assertRaises(TicketAccessDenied):
                await svc.read_conversation(self.who("tutor"), ticket)                           # a different tutor
            with self.assertRaises(TicketAccessDenied):
                await svc.read_conversation(self.who("s2"), ticket)
            with self.assertRaises(TicketAccessDenied):
                await svc.read_conversation(self.who("admin"), ticket)
            await svc.reply(self.who("tutor2"), ticket, "Can you send the exam code? My number is 555 123 4567.")
            view = await svc.view(self.who("s2"), ticket)
            self.assertEqual((view["status"], view["replies"][0]["from"]), ("in_progress", "Tutor"))
            self.assertNotIn("555 123 4567", view["replies"][0]["text"])                         # redacted before storing
            await svc.reply(self.who("s2"), ticket, "It is DB101.")
            with self.assertRaises(TicketAccessDenied):
                await svc.reply(self.who("s1"), ticket, "hi")                                    # another student
            with self.assertRaises(TicketAccessDenied):
                await svc.view(self.who("s1"), ticket)
            with self.assertRaises(TicketAccessDenied):
                await svc.resolve(self.who("tutor"), ticket, "answered", "x")                    # not their ticket
            await svc.resolve(self.who("tutor2"), ticket, "answered", "Marked again.", bot_could_have_answered=False, retrieval_gap=True)
            self.assertEqual((await svc.view(self.who("s2"), ticket))["status"], "resolved")
            self.assertFalse(await svc.reopen(self.who("s1"), ticket))
            self.assertTrue(await svc.reopen(self.who("s2"), ticket))
            self.assertFalse(await svc.reopen(self.who("s2"), ticket))                           # only resolved tickets reopen
            await svc.resolve(self.who("tutor2"), ticket, "referred", "Sent to the exams office.")
            with self.assertRaises(ValueError):
                await svc.resolve(self.who("tutor2"), ticket, "whatever", "x")
            return ticket
        ticket = self.go(run)
        actions = [a[0] for a in self.one("SELECT action FROM audit_log WHERE target = %s ORDER BY id", (f"ticket:{ticket}",))]
        for expected in ("ticket.open", "ticket.claim", "chat.read", "ticket.reply", "ticket.resolve", "ticket.reopened"):
            self.assertIn(expected, actions)
        self.assertEqual(actions.count("chat.read"), 1)                                           # the refused reads were not logged as reads
        row = self.one("SELECT first_response_at IS NOT NULL, bot_could_have_answered, retrieval_gap, outcome FROM escalation_ticket WHERE id = %s", (ticket,))[0]
        self.assertEqual(row, (True, False, True, "referred"))

    def test_restricted_ticket_only_for_designated_tutors(self):
        async def run(dbs, esc, svc):
            ticket = await esc.create(self.ctx(question="thinking about ending it all"), "rule:safety.6")
            self.assertEqual(await svc.queue(self.who("tutor2")), [])
            self.assertFalse(await svc.claim(self.who("tutor2"), ticket))
            self.assertEqual([q["restricted"] for q in await svc.queue(self.who("tutor3"))], [True])
            self.assertTrue(await svc.claim(self.who("tutor3"), ticket))
        self.go(run)

    def test_student_can_cancel_and_ask_for_a_tutor_directly(self):
        async def run(dbs, esc, svc):
            out = await svc.request_tutor(self.who("s2"), self.A["c1"], self.A["conv_s2"], "Call me on 07700 900123 please")
            self.assertTrue(await svc.cancel(self.who("s2"), out.ticket_id))
            self.assertFalse(await svc.cancel(self.who("s2"), out.ticket_id))
            self.assertEqual([t["status"] for t in await svc.my_tickets(self.who("s2"))], ["closed"])
            self.assertEqual(await svc.my_tickets(self.who("s1")), [])
        self.go(run)
        self.assertNotIn("07700", " ".join(r[0] for r in self.one("SELECT content_redacted FROM message")))


class NotificationTests(Rig):
    def run_notifier(self, event_filter=None, channels=None, cfg=None):
        out = {}

        async def run(dbs, esc, svc):
            broker = InMemoryBroker()
            await OutboxRelay(dbs["sb_relay"], broker).run_once()
            notifier = Notifier(dbs["sb_worker"], channels or [out.setdefault("chan", InMemoryChannel())], cfg or self.cfg)
            for d in list(broker.topics.get("escalation.created", [])) + list(broker.topics.get("escalation.updated", [])):
                await notifier.handle(d.message)
            return broker
        return out, run

    def sent(self, chan):
        return {(s[1], s[2]["kind"]) for s in chan.sent}

    def test_created_goes_to_course_tutors_and_not_to_restricted_outsiders(self):
        out, notify = self.run_notifier()

        async def run(dbs, esc, svc):
            await esc.create(self.ctx(), "rule:escalate.4")
            await esc.create(self.ctx("s1", conversation="conv_s1"), "rule:safety.3")
            await notify(dbs, esc, svc)
        self.go(run)
        A = self.A
        sent = [(s[1], s[2]["priority"]) for s in out["chan"].sent]
        self.assertEqual({u for u, p in sent if p == "high"}, {A["tutor"], A["tutor2"], A["tutor3"]})
        self.assertEqual({u for u, p in sent if p == "urgent"}, {A["tutor3"]})                        # designated tutors only
        self.assertTrue(all("question" not in str(s[2]) and "grade" not in str(s[2].get("title", "")) for s in out["chan"].sent))

    def test_no_tutor_falls_back_to_instructor_then_admin_and_urgent_alerts_on_call(self):
        out, notify = self.run_notifier(cfg=EscalationConfig(oncall_user_ids=("on-call-1",)))

        async def run(dbs, esc, svc):
            await esc.create(self.ctx(course="c2", conversation="conv_c2"), "rule:escalate.4")      # c2 has nobody on its roster
            await esc.create(self.ctx(), "rule:safety.1")
            await notify(dbs, esc, svc)
        self.go(run, self.cfg)
        people = {s[1] for s in out["chan"].sent}
        self.assertIn(self.A["admin"], people)                  # nobody on c2 at all: the admin
        self.assertIn("on-call-1", people)                      # urgent
        self.assertIn(self.A["tutor3"], people)

    def test_replies_go_to_the_right_person_and_failures_are_retried_not_lost(self):
        out, _ = self.run_notifier()
        chan = out.setdefault("chan", InMemoryChannel())

        async def run(dbs, esc, svc):
            ticket = await esc.create(self.ctx(), "rule:escalate.4")
            await svc.claim(self.who("tutor2"), ticket)
            await svc.reply(self.who("tutor2"), ticket, "hello")
            await svc.reply(self.who("s2"), ticket, "thanks")
            await svc.resolve(self.who("tutor2"), ticket, "answered", "done")
            broker = InMemoryBroker()
            await OutboxRelay(dbs["sb_relay"], broker).run_once()
            msgs = [d.message for t in ("escalation.created", "escalation.updated") for d in broker.topics.get(t, [])]
            down, up = InMemoryChannel(fail=True), InMemoryChannel()
            n = Notifier(dbs["sb_worker"], [down])
            with self.assertRaises(NotificationFailed):
                await n.handle(msgs[0])
            n.channels = [down, up]                               # falls through to the next channel
            for m in msgs:
                await n.handle(m)
            again = await n.handle(msgs[0])
            self.assertTrue(again["duplicate"])                   # at-least-once delivery: no second notification
            return up
        up = self.go(run)
        kinds = {(s[1], s[2]["kind"]) for s in up.sent}
        self.assertIn((self.A["s2"], "reply"), kinds)
        self.assertIn((self.A["tutor2"], "student_reply"), kinds)
        self.assertIn((self.A["s2"], "resolved"), kinds)
        self.assertIn((self.A["s2"], "claimed"), kinds)


class SlaTests(Rig):
    def age(self, ticket, **cols):
        sets = ", ".join(f"{k} = now() - interval '{v}'" for k, v in cols.items())
        with sup() as c:
            c.execute("SET session_replication_role = replica")
            c.execute(f"UPDATE escalation_ticket SET {sets} WHERE id = %s", (ticket,))

    def test_unclaimed_ticket_is_renotified_then_escalated_to_instructor_then_admin(self):
        async def run(dbs, esc, svc):
            mon = SlaMonitor(dbs["sb_worker"], self.cfg)
            ticket = await esc.create(self.ctx(), "rule:escalate.7")
            self.assertEqual(await mon.run(self.A["id"]), {"renotified": 0, "escalated": 0, "breaches": 0, "closed_no_response": 0})
            levels = []
            for _ in range(5):
                self.age(ticket, created_at="30 hours", last_notified_at="5 hours")
                r = await mon.run(self.A["id"])
                levels.append((r["renotified"], r["escalated"], self.one("SELECT notify_level FROM escalation_ticket WHERE id = %s", (ticket,))[0][0]))
            return levels
        levels = self.go(run)
        self.assertEqual(levels, [(1, 0, 0), (0, 1, 1), (1, 0, 1), (0, 1, 2), (1, 0, 2)])        # tutors x2, instructor x2, then admin
        self.assertEqual(self.one("SELECT count(*) FROM outbox_event WHERE payload->>'kind' = 'escalated'")[0][0], 2)

    def test_claimed_tickets_are_left_alone_and_breach_is_flagged_once(self):
        async def run(dbs, esc, svc):
            mon = SlaMonitor(dbs["sb_worker"], self.cfg)
            ticket = await esc.create(self.ctx(), "rule:escalate.7")
            await svc.claim(self.who("tutor2"), ticket)
            self.age(ticket, created_at="30 hours", last_notified_at="9 hours", sla_deadline="1 hour")
            first = await mon.run(self.A["id"])
            second = await mon.run(self.A["id"])
            return first, second
        first, second = self.go(run)
        self.assertEqual((first["breaches"], first["renotified"], first["escalated"]), (1, 0, 0))
        self.assertEqual(second["breaches"], 0)

    def test_unanswered_tutor_question_closes_the_ticket(self):
        async def run(dbs, esc, svc):
            mon = SlaMonitor(dbs["sb_worker"], self.cfg)
            ticket = await esc.create(self.ctx(), "rule:escalate.7")
            await svc.claim(self.who("tutor2"), ticket)
            await svc.reply(self.who("tutor2"), ticket, "Which exam?")
            self.assertEqual((await mon.run(self.A["id"]))["closed_no_response"], 0)
            return ticket
        ticket = self.go(run)
        with sup() as c:
            c.execute("ALTER TABLE ticket_reply DISABLE TRIGGER ticket_reply_append_only")
            c.execute("UPDATE ticket_reply SET created_at = now() - interval '8 days'")
            c.execute("ALTER TABLE ticket_reply ENABLE TRIGGER ticket_reply_append_only")

        async def again(dbs, esc, svc):
            return await SlaMonitor(dbs["sb_worker"], self.cfg).run(self.A["id"])
        self.assertEqual(self.go(again)["closed_no_response"], 1)
        self.assertEqual(self.one("SELECT status FROM escalation_ticket WHERE id = %s", (ticket,))[0][0], "closed_no_response")


class FaqTests(Rig):
    def rig(self, tags=()):
        store = InMemoryCacheStore()
        cache = SemanticCache(store, HashingEmbedder(), Versions(d1="v1"), policy_version=lambda: "p1")
        return cache

    async def hit(self, cache, question, key="s1"):
        ctx = RequestContext(self.who(key), self.A["c1"], question, "req-12345678")
        ctx.policy_version = "p1"
        return await cache.lookup(ctx)

    def test_approved_answer_is_served_from_cache_then_dropped_when_its_document_changes(self):
        cache = self.rig()

        async def run(dbs, esc, svc):
            faq = FaqService(dbs["sb_app"], cache, SAN)
            ticket = await esc.create(self.ctx(), "rule:escalate.7")
            with self.assertRaises(TicketAccessDenied):
                await faq.promote(self.who("tutor2"), ticket, "How do I request an extension?", "Use the portal.")      # not claimed
            await svc.claim(self.who("tutor2"), ticket)
            with self.assertRaises(TicketAccessDenied):
                await faq.promote(self.who("tutor"), ticket, "How do I request an extension?", "Use the portal.")        # someone else's ticket
            with self.assertRaises(ValueError):
                await faq.promote(self.who("tutor2"), ticket, "How do I ask?", "Email me at ann@example.com")            # personal data
            out = await faq.promote(self.who("tutor2"), ticket, "How do I request an extension?", "Use the extensions portal before the deadline.",
                                    [("d1", "v1")])
            self.assertTrue(out["cached"])
            hit = await self.hit(cache, "How do I request an extension?")
            self.assertEqual(hit.answer, "Use the extensions portal before the deadline.")
            self.assertIsNone(await self.hit(cache, "What is BCNF?"))
            await cache.invalidate_document(self.A["id"], self.A["c1"], "d1")                                              # CDC: the document changed
            self.assertIsNone(await self.hit(cache, "How do I request an extension?"))
            n = await FaqSync(dbs["sb_worker"], cache).reload(self.A["id"])                                                # rebuild from the table
            self.assertEqual(n, 1)
            return ticket
        ticket = self.go(run)
        self.assertEqual(self.one("SELECT approved_faq FROM escalation_ticket WHERE id = %s", (ticket,))[0][0], True)
        self.assertEqual(self.one("SELECT count(*) FROM faq_entry WHERE tenant_id = %s", (self.A["id"],))[0][0], 1)
        self.assertIn("faq.approve", [a[0] for a in self.one("SELECT action FROM audit_log")])

    def test_faq_does_not_cross_courses_or_tenants(self):
        cache = self.rig()

        async def run(dbs, esc, svc):
            faq = FaqService(dbs["sb_app"], cache, SAN)
            ticket = await esc.create(self.ctx(), "rule:escalate.7")
            await svc.claim(self.who("tutor2"), ticket)
            await faq.promote(self.who("tutor2"), ticket, "How do I request an extension?", "Use the portal.")
            other_course = RequestContext(self.who("s2"), self.A["c2"], "How do I request an extension?", "req-12345678")
            other_course.policy_version = "p1"
            self.assertIsNone(await cache.lookup(other_course))
            other_tenant = RequestContext(self.who("s1", "B"), self.B["c1"], "How do I request an extension?", "req-12345678")
            other_tenant.policy_version = "p1"
            self.assertIsNone(await cache.lookup(other_tenant))
        self.go(run)


if __name__ == "__main__":
    unittest.main()
