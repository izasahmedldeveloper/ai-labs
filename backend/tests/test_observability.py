"""LLD 11 test table: metric correctness, no PII in logs, trace completeness and sampling, alert firing, canary,
tenant view isolation, eval gate."""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from studybuddy.observability import (Alert, AlertEngine, Canary, CanaryTarget, EventSink, JsonLogger, MemoryExporter, Registry,
                                      RequestObserver, Telemetry, Thresholds, TraceSampler, add_metrics_routes, chain,
                                      compare_to_baseline, make_record, scrub_value, slis)
from studybuddy.pipeline import Identity, RequestContext, fakes
from tests.test_pipeline import collect, make_ctx, make_orch


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fresh():
    clock = Clock()
    reg = Registry(clock=clock)
    return clock, reg, Telemetry(reg)


class MetricsTests(unittest.TestCase):
    def test_counter_histogram_gauge_and_exposition(self):
        _, reg, _ = fresh()
        c = reg.counter("a_total", "help a", ("tenant",))
        c.inc(tenant="t1"); c.inc(2, tenant="t1"); c.inc(tenant="t2")
        h = reg.histogram("lat_ms", "lat", ("tenant",), buckets=(100, 500))
        for v in (50, 200, 900):
            h.observe(v, tenant="t1")
        g = reg.gauge("g", "gauge")
        g.set(3)
        self.assertEqual(c.value(tenant="t1"), 3)
        self.assertEqual(c.value(), 4)
        text = reg.render()
        self.assertIn('a_total{tenant="t1"} 3', text)
        self.assertIn('lat_ms_bucket{tenant="t1",le="100"} 1', text)
        self.assertIn('lat_ms_bucket{tenant="t1",le="500"} 2', text)
        self.assertIn('lat_ms_bucket{tenant="t1",le="+Inf"} 3', text)
        self.assertIn('lat_ms_sum{tenant="t1"} 1150', text)
        self.assertIn("# TYPE g gauge", text)

    def test_misuse_is_rejected(self):
        _, reg, _ = fresh()
        c = reg.counter("x_total", "", ("tenant",))
        with self.assertRaises(ValueError):
            c.inc(wrong="1")
        with self.assertRaises(ValueError):
            c.inc(-1, tenant="t")
        with self.assertRaises(ValueError):
            reg.counter("bad name", "")
        with self.assertRaises(ValueError):
            reg.gauge("x_total", "", ("tenant",))  # same name, different type

    def test_label_values_are_escaped(self):
        _, reg, _ = fresh()
        reg.counter("e_total", "", ("v",)).inc(v='a"b\\c\nd')
        self.assertIn(r'e_total{v="a\"b\\c\nd"} 1', reg.render())

    def test_windows_and_quantiles(self):
        clock, reg, _ = fresh()
        c = reg.counter("w_total", "")
        h = reg.histogram("w_ms", "")
        c.inc(5); h.observe(1000)
        clock.now += 400
        c.inc(2)
        for v in range(1, 101):
            h.observe(v)
        self.assertEqual(c.window(300), 2)
        self.assertEqual(c.window(1000), 7)
        self.assertEqual(h.quantile(0.95, 300), 95)
        self.assertEqual(h.quantile(0.95), 96)       # 101 samples: the 1000 is only the maximum
        self.assertIsNone(reg.histogram("none_ms", "").quantile(0.5))

    def test_tenant_view_only_shows_that_tenant(self):
        _, reg, t = fresh()
        t.requests.inc(tenant="A", outcome="ok", route="cheap")
        t.requests.inc(tenant="B", outcome="ok", route="cheap")
        t.violations.inc()                                      # platform-wide, no tenant label
        t.stage.observe(5, stage="retriever")                   # platform-wide
        view = reg.render(tenant_id="A")
        self.assertIn('tenant="A"', view)
        self.assertNotIn('tenant="B"', view)
        self.assertNotIn("sb_isolation_violations_total", view)
        self.assertNotIn("sb_stage_ms", view)


class ScrubTests(unittest.TestCase):
    def test_forbidden_keys_raise_and_unknown_keys_are_dropped(self):
        for k in ("question", "answer", "text", "content", "email", "token"):
            with self.assertRaises(ValueError):
                make_record("x", **{k: "hello"})
        rec = make_record("x", tenant="t1", surprise="drop me")
        self.assertNotIn("surprise", rec)
        self.assertEqual(rec["tenant"], "t1")

    def test_values_are_scrubbed(self):
        self.assertNotIn("a@b.com", scrub_value("contact a@b.com now"))
        self.assertNotIn("07700 900123", scrub_value("call 07700 900123"))
        self.assertIn("[REDACTED]", scrub_value("k=" + "A" * 50))
        self.assertLessEqual(len(scrub_value("x" * 5000)), 300)

    def test_logger_writes_json_and_never_raises(self):
        lines = []
        JsonLogger(lines.append, clock=lambda: 5).log("e", tenant="t1", error="boom a@b.com")
        rec = json.loads(lines[0])
        self.assertEqual((rec["event"], rec["tenant"], rec["ts"]), ("e", "t1", 5))
        self.assertNotIn("a@b.com", lines[0])

        def broken(_):
            raise RuntimeError("disk full")
        JsonLogger(broken).log("e")  # must not raise


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, **orch_kw):
        self.reg = Registry()
        self.t = Telemetry(self.reg)
        self.lines, self.exp = [], MemoryExporter()
        self.obs = RequestObserver(self.reg, logger=JsonLogger(self.lines.append), exporter=self.exp,
                                   sampler=TraceSampler(rate=1.0), telemetry=self.t)
        return make_orch(on_complete=self.obs.on_complete, **orch_kw)

    async def run_one(self, orch, ctx):
        events = await collect(orch, ctx)
        await asyncio.sleep(0.05)
        return events

    async def test_request_metrics_log_and_trace(self):
        orch = self.setup()
        ctx = make_ctx(question="My secret question about jane@example.com")
        await self.run_one(orch, ctx)
        self.assertEqual(self.t.requests.value(tenant="t1", outcome="ok"), 1)
        self.assertEqual(self.t.latency.count(tenant="t1"), 1)
        self.assertGreater(self.t.stage.count(), 0)
        self.assertEqual(len(self.exp.traces), 1)
        tr = self.exp.traces[0]
        self.assertEqual((tr.trace_id, tr.request_id), (ctx.trace_id, "r1"))
        names = [s.name for s in tr.spans]
        self.assertEqual(names[0], "chat.request")
        self.assertEqual(len(names) - 1, len(ctx.timings))            # every stage has a span
        blob = "\n".join(self.lines) + json.dumps([s.attrs for s in tr.spans], default=str)
        self.assertNotIn("secret question", blob)
        self.assertNotIn("jane@example.com", blob)
        self.assertNotIn("Normalization removes redundancy", blob)    # the answer is not logged either
        rec = json.loads(self.lines[0])
        self.assertEqual((rec["event"], rec["outcome"], rec["tenant"]), ("chat.request", "ok", "t1"))

    async def test_model_usage_and_cache_metrics(self):
        _, reg, t = fresh()
        obs = RequestObserver(reg, telemetry=t)
        ctx = make_ctx()
        ctx.outcome, ctx.cache_result, ctx.route = "ok", "miss", "cheap_llm"
        ctx.model_call = {"tokens_in": 100, "tokens_out": 40, "cost_usd": 0.002, "ttft_ms": 800, "failover": True, "provider": "anthropic"}
        obs.on_complete(ctx)
        self.assertEqual((t.tok_in.value(), t.tok_out.value()), (100, 40))
        self.assertAlmostEqual(t.cost.value(tenant="t1"), 0.002)
        self.assertEqual(t.ttft.quantile(0.5), 800)
        self.assertEqual(t.failover.value(), 1)
        self.assertEqual(t.cache.value(result="miss"), 1)

    async def test_blocked_and_error_and_escalated_counted_and_always_traced(self):
        _, reg, t = fresh()
        exp = MemoryExporter()
        obs = RequestObserver(reg, exporter=exp, sampler=TraceSampler(rate=0.0), telemetry=t)
        for outcome in ("blocked", "error", "escalated", "ok"):
            c = make_ctx(request_id=outcome); c.outcome = outcome
            obs.on_complete(c)
        self.assertEqual(sorted(x.outcome for x in exp.traces), ["blocked", "error", "escalated"])   # ok at rate 0: dropped
        self.assertEqual((t.guard.value(), t.escalations.value()), (1, 1))

    def test_sampler(self):
        s = TraceSampler(rate=0.1, slow_ms=6000)
        self.assertEqual(s.decide("ok", 7000, "abc"), "slow")
        self.assertIsNone(TraceSampler(rate=0.0).decide("ok", 10, "abc"))
        self.assertEqual(TraceSampler(rate=1.0).decide("ok", 10, "abc"), "sampled")
        kept = sum(1 for i in range(2000) if s.decide("ok", 10, f"trace-{i}"))
        self.assertTrue(120 < kept < 280, kept)                      # about 10%
        self.assertEqual(s.decide("ok", 10, "same"), s.decide("ok", 10, "same"))  # stable
        with self.assertRaises(ValueError):
            TraceSampler(rate=2)

    async def test_failing_hook_does_not_touch_the_answer_and_chain_isolates(self):
        calls = []

        def boom(ctx):
            raise RuntimeError("x")

        async def second(ctx):
            calls.append(ctx.outcome)
        orch = make_orch(on_complete=chain(boom, second))
        events = await self.run_one(orch, make_ctx())
        self.assertEqual(events[-1].type, "done")
        self.assertEqual(calls, ["ok"])
        _, reg, _ = fresh()
        obs = RequestObserver(reg, logger=JsonLogger(lambda s: 1 / 0))
        obs.on_complete(object())     # malformed context: swallowed


class EventSinkTests(unittest.TestCase):
    def test_events_counted_and_violations_flagged(self):
        _, reg, t = fresh()
        lines = []
        sink = EventSink(reg, logger=JsonLogger(lines.append), telemetry=t)
        sink({"stage": "retriever", "decision": "isolation_violation", "chunk_id": "c9"})
        sink({"stage": "input_guardrails", "decision": "block", "rule_id": "r1"})
        sink({"event": "ingest.retry", "version_id": "v1", "attempt": 1, "error": "TemporaryError"})
        sink({"event": "ticket.created", "ticket_id": "t1", "priority": "urgent", "trigger": "safety"})
        self.assertEqual(t.violations.value(), 1)
        self.assertEqual(t.events.value(source="ingest", decision="retry"), 1)
        self.assertEqual(t.events.value(source="ticket", decision="created"), 1)
        sink.dead_letter({"reason": "gave up"})
        self.assertEqual(t.dead_letters.value(), 1)
        self.assertEqual(json.loads(lines[0])["level"], "error")
        sink({"weird": object()})     # malformed: ignored
        self.assertNotIn("chunk_id", "".join(lines))


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.reg, self.t = fresh()
        self.sent = []
        self.eng = AlertEngine(self.reg, self.t, Thresholds(), notify=lambda a, s: self.sent.append((a.name, s)))

    def traffic(self, ok, bad, tenant="t1"):
        self.t.requests.inc(ok, tenant=tenant, outcome="ok", route="cheap")
        if bad:
            self.t.requests.inc(bad, tenant=tenant, outcome="error", route="cheap")

    def names(self):
        return {a.name for a in self.eng.evaluate()}

    def test_quiet_when_healthy(self):
        self.traffic(500, 1)
        self.assertEqual(self.names(), set())

    def test_availability_burn_pages_then_resolves_and_notifies_once(self):
        self.traffic(80, 20)                        # 20% errors against a 0.5% budget
        self.assertIn("availability_burn", self.names())
        self.assertIn("availability_burn", self.names())
        self.assertEqual(self.sent.count(("availability_burn", "firing")), 1)
        self.clock.now += 4000                      # outside both windows
        self.assertNotIn("availability_burn", self.names())
        self.assertIn(("availability_burn", "resolved"), self.sent)

    def test_small_samples_and_busy_do_not_page(self):
        self.traffic(2, 3)
        self.assertEqual(self.names(), set())
        self.t.requests.inc(500, tenant="t1", outcome="busy", route="none")
        self.assertEqual(self.names(), set())

    def test_stale_burn_that_already_ended_does_not_page(self):
        self.traffic(0, 50)                          # an old outage
        self.clock.now += 400                        # beyond the 5 minute window
        self.traffic(100, 0)
        self.assertNotIn("availability_burn", self.names())

    def test_short_spike_inside_a_healthy_hour_does_not_page(self):
        self.traffic(10000, 0)
        self.clock.now += 1800
        self.traffic(20, 30)                         # bad for five minutes, but only 0.3% of the hour
        self.assertNotIn("availability_burn", self.names())

    def test_latency_percentiles(self):
        for _ in range(30):
            self.t.ttft.observe(3000, tenant="t1"); self.t.latency.observe(9000, tenant="t1")
        self.assertTrue({"ttft_p95_high", "latency_p95_high"} <= self.names())
        _, reg, t = fresh()
        for _ in range(5):
            t.ttft.observe(9000, tenant="t1")
        self.assertEqual(AlertEngine(reg, t).check(), [])      # too few samples

    def test_any_violation_pii_dead_letter_urgent_and_canary_page(self):
        self.t.violations.inc(); self.t.pii_leaks.inc(); self.t.dead_letters.inc()
        self.t.urgent_unclaimed.set(2); self.t.canary.inc(result="fail")
        self.assertTrue({"cross_tenant_violation", "pii_in_logs", "ingest_dead_letter", "urgent_ticket_unclaimed",
                         "canary_failing"} <= self.names())
        self.t.urgent_unclaimed.set(0)
        self.assertNotIn("urgent_ticket_unclaimed", self.names())

    def test_spend_anomaly_and_block_spike_and_failover(self):
        for h in range(3):                          # three normal earlier hours
            self.clock.now += 3600
            self.t.cost.inc(1.0, tenant="t1")
        self.clock.now += 3600
        self.t.cost.inc(10.0, tenant="t1")
        self.assertIn("spend_anomaly", self.names())
        _, reg, t = fresh()
        t.requests.inc(100, tenant="t1", outcome="blocked", route="none"); t.guard.inc(60, tenant="t1")
        t.failover.inc(80, tenant="t1")
        names = {a.name for a in AlertEngine(reg, t).check()}
        self.assertTrue({"guardrail_block_spike", "provider_failover"} <= names)

    def test_notify_failure_is_ignored_and_slis(self):
        eng = AlertEngine(self.reg, self.t, notify=lambda a, s: 1 / 0)
        self.t.violations.inc()
        self.assertEqual(len(eng.evaluate()), 1)
        self.traffic(99, 1)
        s = slis(self.t)
        self.assertEqual(s["requests"], 100)
        self.assertAlmostEqual(s["availability"], 0.99)


class CanaryTests(unittest.IsolatedAsyncioTestCase):
    def targets(self, a_answer, b_answer, a_outcome="ok", cites=True):
        async def ask_a(q):
            return {"answer": a_answer, "citations": ["c1"] if cites else [], "outcome": a_outcome}

        async def ask_b(q):
            return {"answer": b_answer, "citations": ["c2"], "outcome": "ok"}
        return [CanaryTarget("A", ask_a, "q", "alpha fact", "BETA-MARKER"), CanaryTarget("B", ask_b, "q", "beta fact", "ALPHA-MARKER")]

    async def test_pass(self):
        _, _, t = fresh()
        res = await Canary(self.targets("The alpha fact is 1", "The beta fact is 2"), t).run()
        self.assertTrue(res.ok, res.problems)
        self.assertEqual(t.canary.value(result="pass"), 1)

    async def test_isolation_failure_counts_a_violation(self):
        _, _, t = fresh()
        res = await Canary(self.targets("alpha fact and BETA-MARKER", "beta fact"), t).run()
        self.assertFalse(res.ok)
        self.assertEqual(t.violations.value(), 1)
        self.assertEqual(t.canary.value(result="fail"), 1)

    async def test_missing_fact_citation_outcome_and_errors(self):
        _, _, t = fresh()
        res = await Canary(self.targets("nothing", "beta fact", a_outcome="error", cites=False), t).run()
        self.assertEqual(len(res.problems), 3)

        async def dies(q):
            raise ConnectionError()
        res = await Canary([CanaryTarget("A", dies, "q", "x", "y")], t).run()
        self.assertFalse(res.ok)

        async def hangs(q):
            await asyncio.sleep(5)
        res = await Canary([CanaryTarget("A", hangs, "q", "x", "y")], t, timeout_s=0.05).run()
        self.assertFalse(res.ok)

        slow = Canary(self.targets("alpha fact", "beta fact"), t, max_ms=-1)
        self.assertTrue(any("slow" in p for p in (await slow.run()).problems))


class EvalGateTests(unittest.TestCase):
    base = {"faithfulness": 0.90, "leak_rate": 0.0, "p95_latency_ms": 3000, "retrieval_recall": 0.8}

    def test_pass_within_tolerance(self):
        r = compare_to_baseline({"faithfulness": 0.88, "leak_rate": 0.0, "p95_latency_ms": 3400, "retrieval_recall": 0.85}, self.base)
        self.assertTrue(r.passed, r)

    def test_regressions_fail(self):
        r = compare_to_baseline({"faithfulness": 0.80, "leak_rate": 0.01, "p95_latency_ms": 4000, "retrieval_recall": 0.8}, self.base)
        self.assertFalse(r.passed)
        self.assertEqual(len(r.regressions), 3)

    def test_missing_metric_fails(self):
        r = compare_to_baseline({"faithfulness": 0.9}, self.base)
        self.assertFalse(r.passed)
        self.assertIn("leak_rate", r.missing)

    def test_boundary_is_allowed(self):
        self.assertTrue(compare_to_baseline({"faithfulness": 0.87, "leak_rate": 0.0, "p95_latency_ms": 3500, "retrieval_recall": 0.8}, self.base).passed)


class RouteTests(unittest.TestCase):
    def client(self, token="s3cret"):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        class U:
            def __init__(self, role, tenant):
                self.identity = Identity(tenant, "u", role)

        def authenticate(request):
            return U(request.headers.get("x-role", "student"), request.headers.get("x-tenant", "A"))
        _, reg, t = fresh()
        t.requests.inc(tenant="A", outcome="ok", route="r"); t.requests.inc(tenant="B", outcome="ok", route="r")
        app = FastAPI()
        add_metrics_routes(app, reg, authenticate=authenticate, scrape_token=token)
        return TestClient(app)

    def test_platform_metrics_need_the_token(self):
        c = self.client()
        self.assertEqual(c.get("/metrics").status_code, 404)
        self.assertEqual(c.get("/metrics", headers={"authorization": "Bearer wrong"}).status_code, 404)
        r = c.get("/metrics", headers={"authorization": "Bearer s3cret"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('tenant="B"', r.text)
        self.assertEqual(self.client(token=None).get("/metrics", headers={"authorization": "Bearer "}).status_code, 404)

    def test_tenant_view_is_limited_to_own_tenant_and_to_staff(self):
        c = self.client()
        self.assertEqual(c.get("/tenant/metrics").status_code, 404)                       # a student
        r = c.get("/tenant/metrics", headers={"x-role": "tenant_admin", "x-tenant": "A"})
        self.assertIn('tenant="A"', r.text)
        self.assertNotIn('tenant="B"', r.text)


if __name__ == "__main__":
    unittest.main()
