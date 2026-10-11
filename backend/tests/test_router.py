"""Router tests (LLD 07 s10). Stdlib unittest; no network."""
import asyncio
import time
import unittest

from studybuddy.guardrails import RuleInputGuardrails, RuleOutputGuardrails
from studybuddy.pipeline import BudgetState, Identity, Orchestrator, PipelineConfig, RequestContext, fakes
from studybuddy.pipeline.types import Route
from studybuddy.retrieval.fakes import HashingEmbedder
from studybuddy.router import (CentroidIntentClassifier, Intent, LexiconIntentClassifier, Router, RouterConfig,
                               RouterExample, ShadowRouter, complexity, evaluate)
from studybuddy.router.types import IntentVerdict


def run(coro):
    return asyncio.run(coro)


def ctxf(q, history=(), budget=BudgetState.OK):
    return RequestContext(identity=Identity("t1", "u1", "student"), course_id="c1", question=q,
                          request_id="req-00001", history=history, budget_state=budget)


def route(q, router=None, **kw):
    r = router or Router()
    ctx = ctxf(q, **kw)
    return run(r.route(ctx)), ctx


class RuleTests(unittest.TestCase):
    def test_greetings_thanks_help_are_rules_without_retrieval(self):
        for q in ("hi", "Hello!", "hey there", "thanks", "Thank you!", "bye", "help", "who are you?"):
            d, ctx = route(q)
            self.assertEqual((d.route, d.needs_retrieval), (Route.RULE, False), q)
            self.assertTrue(d.canned_answer)
            self.assertEqual(ctx.router_trace["tier"], 1)

    def test_empty_input_asks_for_more(self):
        d, _ = route("   ?!  ")
        self.assertEqual(d.route, Route.REDIRECT)

    def test_a_one_word_topic_is_still_a_question(self):
        d, _ = route("BCNF")
        self.assertIn(d.route, (Route.CHEAP_LLM,))  # not swallowed by a rule (deviation from LLD 07 s3, documented)

    def test_course_questions_are_not_escalated(self):
        for q in ("What is BCNF?", "How do I cancel my course?", "What is a file extension in an operating system?",
                  "Explain deadlock", "How do I drop a class?", "What is the abstract of week 3?"):
            d, _ = route(q)
            self.assertIn(d.route, (Route.CHEAP_LLM, Route.STRONG_LLM), q)

    def test_sensitive_phrases_go_to_a_human(self):
        for q in ("I want to dispute my grade", "I'd like to appeal my mark", "my grade is wrong",
                  "I want to file a complaint", "I am being harassed by another student", "I will contact a lawyer",
                  "I need an extension on my assignment", "there was a family emergency", "I want a refund"):
            d, ctx = route(q)
            self.assertEqual(d.route, Route.HUMAN, q)
            self.assertTrue(ctx.router_trace["rule"].startswith("rule:escalate"), q)


class SafetyTests(unittest.TestCase):
    PHRASES = ("I want to kill myself", "i am thinking of ending it", "I want to die", "I don't want to live anymore",
               "thinking about suicide", "ｓｕｉｃｉｄｅ", "I can’t go on", "I want to end my life", "i might hurt myself")

    def test_safety_phrases_take_the_safe_path_with_a_supportive_message(self):
        for q in self.PHRASES:
            d, _ = route(q)
            self.assertEqual(d.route, Route.HUMAN, q)
            self.assertIn("emergency", d.canned_answer)
            self.assertFalse(d.needs_retrieval)

    def test_safety_rules_do_not_depend_on_the_classifier_or_embedder(self):
        class Broken:
            version, needs_embedding = "x", True

            def classify(self, *a):
                raise RuntimeError("model down")

        class BrokenEmbedder:
            async def embed(self, texts):
                raise RuntimeError("embedder down")

        r = Router(Broken(), embedder=BrokenEmbedder())
        for q in self.PHRASES:
            self.assertEqual(route(q, r)[0].route, Route.HUMAN, q)

    def test_safety_beats_everything_else_in_the_message(self):
        d, _ = route("thanks, I want to kill myself")
        self.assertEqual(d.route, Route.HUMAN)


class ClassifierTests(unittest.TestCase):
    def test_soft_sensitive_goes_to_human_and_off_topic_is_redirected(self):
        d, ctx = route("I'm so stressed and overwhelmed I can't cope with this module")
        self.assertEqual(d.route, Route.HUMAN)
        self.assertEqual(ctx.router_trace["intent"], "sensitive")
        d, _ = route("Who won the football world cup?")
        self.assertEqual((d.route, d.needs_retrieval), (Route.REDIRECT, False))
        d, _ = route("tell me a joke")
        self.assertEqual(d.route, Route.REDIRECT)

    def test_low_confidence_redirect_falls_through_but_sensitive_threshold_is_low(self):
        class Fixed:
            version, needs_embedding = "fixed", False

            def __init__(self, intent, conf):
                self.v = IntentVerdict(intent, conf, "fixed")

            def classify(self, *a):
                return self.v

        d, _ = route("What is BCNF?", Router(Fixed(Intent.OUT_OF_SCOPE, 0.4)))
        self.assertEqual(d.route, Route.CHEAP_LLM)  # not sure enough to turn a student away
        d, _ = route("What is BCNF?", Router(Fixed(Intent.SENSITIVE, 0.4)))
        self.assertEqual(d.route, Route.HUMAN)  # recall over precision for human cases

    def test_classifier_failure_falls_back_to_complexity(self):
        class Broken:
            version, needs_embedding = "x", False

            def classify(self, *a):
                raise RuntimeError("boom")

        d, ctx = route("What is BCNF?", Router(Broken()))
        self.assertEqual(d.route, Route.CHEAP_LLM)
        self.assertTrue(ctx.router_trace["classifier_error"])

    def test_unclear_can_ask_for_clarification(self):
        r = Router(config=RouterConfig(unclear_action="clarify"))
        d, _ = route("databases", r)
        self.assertEqual(d.route, Route.REDIRECT)
        self.assertEqual(route("databases")[0].route, Route.CHEAP_LLM)  # default: let retrieval decide

    def test_centroid_classifier_trains_on_the_shared_embedding(self):
        emb = HashingEmbedder()
        data = {Intent.SENSITIVE: ["i want to appeal the committee decision", "my coursework was marked unfairly again"],
                Intent.COURSE_QUESTION: ["what is a candidate key in bcnf", "explain third normal form with an example"]}
        vecs = {i: run(emb.embed(t)) for i, t in data.items()}
        clf = CentroidIntentClassifier.fit((i, v) for i, vs in vecs.items() for v in vs)
        v = run(emb.embed(["the committee decision on my coursework was unfair"]))[0]
        self.assertEqual(clf.classify("", v).intent, Intent.SENSITIVE)
        again = CentroidIntentClassifier.fit((i, v) for i, vs in vecs.items() for v in vs)
        self.assertEqual(clf.version, again.version)
        other = CentroidIntentClassifier.fit([(Intent.SENSITIVE, vecs[Intent.SENSITIVE][0]),
                                              (Intent.COURSE_QUESTION, vecs[Intent.COURSE_QUESTION][0])])
        self.assertNotEqual(clf.version, other.version)
        with self.assertRaises(ValueError):
            clf.classify("x", None)
        with self.assertRaises(ValueError):
            CentroidIntentClassifier.fit([(Intent.SENSITIVE, v)])
        ctx = ctxf("the committee decision on my coursework was unfair")
        decision = run(Router(clf, embedder=emb).route(ctx))
        self.assertEqual(decision.route, Route.HUMAN)
        self.assertIsNotNone(ctx.question_embedding)  # computed once, shared with cache/retriever


class ComplexityTests(unittest.TestCase):
    def test_simple_definition_is_cheap_and_multi_step_is_strong(self):
        self.assertEqual(route("What is BCNF?")[0].route, Route.CHEAP_LLM)
        self.assertEqual(route("Define a primary key")[0].route, Route.CHEAP_LLM)
        d, ctx = route("Derive the lossless join condition step by step and compare BCNF with 3NF")
        self.assertEqual(d.route, Route.STRONG_LLM)
        self.assertIn("reasoning", ctx.router_trace["signals"])
        self.assertEqual(route("Compare B-tree and hash indexes and explain why one is better for range queries")[0].route,
                         Route.STRONG_LLM)

    def test_long_but_plain_question_is_not_automatically_strong(self):
        q = " ".join(["tell"] * 5 + ["me"] * 5 + ["about"] * 5 + ["normalization"] * 20)
        self.assertEqual(route(q)[0].route, Route.CHEAP_LLM)

    def test_deep_conversation_adds_a_little(self):
        a = complexity("Explain why BCNF is stricter than 3NF").score
        b = complexity("Explain why BCNF is stricter than 3NF", history_len=8).score
        self.assertGreater(b, a)

    def test_strong_threshold_is_configurable(self):
        r = Router(config=RouterConfig(strong_threshold=0.2))
        self.assertEqual(route("Explain why BCNF is stricter than 3NF", r)[0].route, Route.STRONG_LLM)


class LatencyTests(unittest.TestCase):
    def test_p95_is_far_inside_the_50ms_budget(self):
        r = Router()
        questions = ["What is BCNF?", "hi", "Derive the join dependency step by step and compare it with 4NF",
                     "who won the football world cup", "I want to dispute my grade"] * 60
        times = []
        for q in questions:
            t0 = time.perf_counter()
            run(r.route(ctxf(q)))
            times.append(time.perf_counter() - t0)
        times.sort()
        self.assertLess(times[int(len(times) * 0.95)], 0.05)


class ShadowTests(unittest.TestCase):
    def test_live_decision_wins_and_disagreements_are_recorded(self):
        seen = []
        shadow = ShadowRouter(Router(), Router(config=RouterConfig(strong_threshold=0.1)),
                              lambda ctx, live, cand: seen.append((live.route, cand.route if cand else None)))
        ctx = ctxf("Explain why BCNF is stricter than 3NF")
        decision = run(shadow.route(ctx))
        self.assertEqual(decision.route, Route.CHEAP_LLM)
        self.assertEqual(seen, [(Route.CHEAP_LLM, Route.STRONG_LLM)])
        self.assertEqual(ctx.router_trace["tier"], 3)
        self.assertLess(ctx.router_trace["complexity"], 0.6)  # the candidate did not overwrite the live trace

    def test_a_broken_candidate_or_sink_never_affects_the_user(self):
        class Boom:
            async def route(self, ctx):
                raise RuntimeError("bad candidate")

        seen = []
        shadow = ShadowRouter(Router(), Boom(), lambda ctx, live, cand: seen.append(cand))
        self.assertEqual(run(shadow.route(ctxf("What is BCNF?"))).route, Route.CHEAP_LLM)
        self.assertEqual(seen, [None])
        shadow = ShadowRouter(Router(), Router(), lambda *a: 1 / 0)
        self.assertEqual(run(shadow.route(ctxf("What is BCNF?"))).route, Route.CHEAP_LLM)


# a small SYNTHETIC labelled set: proves the harness and gives a baseline, says nothing about real students
LABELLED = [
    RouterExample("hi", "rule"), RouterExample("thanks!", "rule"), RouterExample("what can you do", "rule"),
    RouterExample("what is BCNF", "cheap_llm"), RouterExample("define a foreign key", "cheap_llm"),
    RouterExample("how do I drop a class", "cheap_llm"),
    RouterExample("derive the normal form step by step and compare with BCNF", "strong_llm"),
    RouterExample("who won the football world cup", "redirect"), RouterExample("what's the weather", "redirect"),
    RouterExample("I want to dispute my grade", "human"), RouterExample("I need an extension because of a family emergency", "human"),
    RouterExample("I want to kill myself", "human"), RouterExample("I am being harassed", "human"),
    RouterExample("I feel overwhelmed and can't cope", "human"), RouterExample("my mark is wrong", "human"),
]


class EvaluationTests(unittest.TestCase):
    def test_report_has_per_route_precision_recall_and_the_sensitive_target(self):
        rep = evaluate(Router(), LABELLED)
        self.assertEqual(rep.n, len(LABELLED))
        self.assertEqual(rep.recall("human"), 1.0)
        self.assertTrue(rep.meets_sensitive_target)
        self.assertEqual(rep.precision("human"), 1.0)
        self.assertEqual(rep.misses, [])

    def test_missed_sensitive_cases_fail_the_target(self):
        rep = evaluate(Router(), LABELLED + [RouterExample("the committee ruled against me and I think it's corrupt", "human")])
        self.assertLess(rep.human_recall, 1.0)
        self.assertFalse(rep.meets_sensitive_target)
        self.assertEqual(len(rep.misses), 1)


# ------------------------------------------------------------------ through the real pipeline
def pipeline(router, generator=None, **cfg):
    from studybuddy.retrieval import HybridRetriever, InMemoryChunkStore, StoredChunk
    emb = HashingEmbedder()
    store = InMemoryChunkStore()
    ch = StoredChunk("t1-c1-bcnf", "BCNF is a stricter form of third normal form. Every determinant must be a candidate key.",
                     "t1", "c1", "d1", "v1", ordinal=0, page=1, heading="Notes")
    store.add([ch], emb.embed_sync([ch.text]))
    store.set_current("t1", "c1", "d1", "v1")
    gen = generator or fakes.FakeGenerator()
    esc = fakes.FakeEscalation()
    orch = Orchestrator(guardrails=RuleInputGuardrails(), cache=fakes.FakeCache(), router=router,
                        retriever=HybridRetriever(store, emb), generator=gen,
                        output_guardrails=RuleOutputGuardrails(), escalation=esc, config=PipelineConfig(**cfg))
    return orch, gen, esc


async def ask(orch, q, rid="req-00001"):
    ctx = ctxf(q)
    ctx.__dict__["request_id"] = rid
    return ctx, [e async for e in orch.handle(ctx)]


class PipelineTests(unittest.TestCase):
    def test_greeting_uses_no_model_and_no_retrieval(self):
        async def go():
            orch, gen, esc = pipeline(Router())
            ctx, events = await ask(orch, "hi")
            self.assertEqual(gen.calls, 0)
            self.assertEqual(ctx.route, "rule")
            self.assertEqual(events[-1].data["sources"], [])
            self.assertNotIn("router_trace", events[-1].data)
        run(go())

    def test_safety_message_is_shown_and_a_ticket_is_created(self):
        async def go():
            orch, gen, esc = pipeline(Router())
            ctx, events = await ask(orch, "I want to kill myself")
            self.assertEqual(gen.calls, 0)
            self.assertEqual(ctx.outcome, "escalated")
            text = "".join(e.data["text"] for e in events if e.type == "token")
            self.assertIn("emergency", text)
            self.assertNotIn("passed this to your tutor", text)
            self.assertTrue(any(e.type == "ticket" for e in events))
        run(go())

    def test_ordinary_sensitive_case_keeps_the_default_tutor_message(self):
        async def go():
            orch, gen, esc = pipeline(Router())
            ctx, events = await ask(orch, "I want to dispute my grade")
            text = "".join(e.data["text"] for e in events if e.type == "token")
            self.assertIn("passed this to your tutor", text)
        run(go())

    def test_router_timeout_uses_default_route_and_answer_still_arrives(self):
        async def go():
            orch, gen, _ = pipeline(fakes.FakeRouter(delay=0.5))
            ctx, events = await ask(orch, "What is BCNF?")
            self.assertEqual(ctx.route, "cheap_llm")
            self.assertEqual(events[-1].type, "done")
            self.assertEqual(ctx.outcome, "ok")
        run(go())

    def test_chitchat_is_redirected_without_the_model(self):
        async def go():
            orch, gen, _ = pipeline(Router())
            ctx, events = await ask(orch, "Who won the football world cup?")
            self.assertEqual((ctx.route, gen.calls), ("redirect", 0))
        run(go())

    def test_strong_question_is_demoted_at_the_soft_budget_limit(self):
        async def go():
            orch, gen, _ = pipeline(Router())
            ctx = ctxf("Derive the lossless join condition step by step and compare BCNF with 3NF", budget=BudgetState.SOFT_LIMIT)
            [e async for e in orch.handle(ctx)]
            self.assertEqual(ctx.route, "cheap_llm")
        run(go())

    def test_router_trace_is_internal_only(self):
        async def go():
            orch, gen, _ = pipeline(Router())
            ctx, events = await ask(orch, "What is BCNF?")
            self.assertIn("complexity", ctx.router_trace)
            self.assertNotIn("complexity", str([e.data for e in events]))
        run(go())

    def test_under_routing_signal_when_a_cheap_answer_fails_the_final_check(self):
        async def go():
            orch, gen, _ = pipeline(Router(), fakes.FakeGenerator(text="Bananas are yellow fruit grown in warm places."))
            ctx, events = await ask(orch, "What is BCNF?")
            if ctx.notices:
                self.assertTrue(ctx.router_trace.get("cheap_failed_final_check"))
            else:  # the output guardrail passed it; make sure the flag is not set spuriously
                self.assertNotIn("cheap_failed_final_check", ctx.router_trace)
        run(go())


if __name__ == "__main__":
    unittest.main()
