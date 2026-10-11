"""OpenAI adapter tests against httpx.MockTransport only, plus the OpenAI-primary / Anthropic-fallback wiring."""
import asyncio
import json
import os
import unittest
from unittest import mock

try:
    import httpx
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("httpx not installed")

from studybuddy.modelgateway import ChatMessage, Delta, Done, ProviderError
from studybuddy.modelgateway.providers.openai import OpenAIChatProvider
from studybuddy.modelgateway.types import ProviderRequest

SSE = "\n".join([
    'data: {"choices":[{"delta":{"role":"assistant"}}]}', '',
    'data: {"choices":[{"delta":{"content":"Hello "}}]}', '',
    'data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}]}', '',
    'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":5}}', '',
    'data: [DONE]', ''])
REQ = ProviderRequest("m1", "system text", (ChatMessage("user", "hi"),), 100)


def collect(provider):
    async def go():
        return [e async for e in provider.stream(REQ)]
    return asyncio.run(go())


class OpenAIProviderTests(unittest.TestCase):
    def test_streams_text_and_usage_and_sends_expected_request(self):
        seen = {}

        def handler(request):
            seen["headers"], seen["body"] = request.headers, json.loads(request.content)
            return httpx.Response(200, text=SSE)

        events = collect(OpenAIChatProvider("sk-test", transport=httpx.MockTransport(handler)))
        self.assertEqual("".join(e.text for e in events if isinstance(e, Delta)), "Hello world")
        done = events[-1]
        self.assertIsInstance(done, Done)
        self.assertEqual((done.usage.input_tokens, done.usage.output_tokens, done.stop_reason), (12, 5, "stop"))
        self.assertEqual(seen["headers"]["authorization"], "Bearer sk-test")
        self.assertEqual(seen["body"]["messages"][0], {"role": "system", "content": "system text"})
        self.assertEqual(seen["body"]["messages"][1]["content"], "hi")
        self.assertTrue(seen["body"]["stream_options"]["include_usage"])

    def test_errors_map_and_never_leak_the_key(self):
        for status, kind in ((429, "rate_limit"), (401, "auth"), (400, "bad_request"), (503, "server")):
            p = OpenAIChatProvider("sk-secret-key", transport=httpx.MockTransport(
                lambda r, s=status: httpx.Response(s, headers={"retry-after": "2"}, text="sk-secret-key prompt hi")))
            with self.assertRaises(ProviderError) as cm:
                collect(p)
            self.assertEqual(cm.exception.kind, kind)
            self.assertNotIn("sk-secret-key", str(cm.exception))
            self.assertNotIn("prompt", str(cm.exception))
        self.assertNotIn("sk-secret-key", repr(p))

    def test_connection_failure(self):
        def boom(request):
            raise httpx.ConnectError("no route")
        with self.assertRaises(ProviderError) as cm:
            collect(OpenAIChatProvider("k", transport=httpx.MockTransport(boom)))
        self.assertEqual(cm.exception.kind, "connection")


class WiringTests(unittest.TestCase):
    def test_openai_primary_anthropic_fallback(self):
        from studybuddy.api.dev import build_dev_gateway
        gw = build_dev_gateway("o", "a")
        for logical in ("chat-cheap", "chat-strong"):
            route = gw.routing.routes[logical]
            self.assertEqual((route.primary.provider, route.fallback.provider), ("openai", "anthropic"))

    def test_single_key_means_no_fallback(self):
        from studybuddy.api.dev import build_dev_gateway
        route = build_dev_gateway(None, "a").routing.routes["chat-cheap"]
        self.assertEqual((route.primary.provider, route.fallback), ("anthropic", None))
        route = build_dev_gateway("o", None).routing.routes["chat-strong"]
        self.assertEqual((route.primary.provider, route.fallback), ("openai", None))

    def test_no_keys_uses_the_canned_model(self):
        from studybuddy.api import dev
        from studybuddy.pipeline import fakes
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertIsInstance(dev.build_dev_orchestrator().generator, fakes.FakeGenerator)


if __name__ == "__main__":
    unittest.main()
