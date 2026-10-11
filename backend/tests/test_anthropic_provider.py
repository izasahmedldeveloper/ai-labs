"""Anthropic adapter tests against httpx.MockTransport only. Nothing here touches the real API."""
import asyncio
import json
import unittest

try:
    import httpx
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("httpx not installed")

from studybuddy.modelgateway import ChatMessage, Delta, Done, ProviderError
from studybuddy.modelgateway.providers.anthropic import AnthropicChatProvider
from studybuddy.modelgateway.types import ProviderRequest

SSE = "\n".join([
    'event: message_start', 'data: {"type":"message_start","message":{"usage":{"input_tokens":12}}}', '',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello "}}', '',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"world"}}', '',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}', '',
    'data: {"type":"message_stop"}', ''])
REQ = ProviderRequest("m1", "system text", (ChatMessage("user", "hi"),), 100)


def collect(provider):
    async def go():
        return [e async for e in provider.stream(REQ)]
    return asyncio.run(go())


class AnthropicProviderTests(unittest.TestCase):
    def test_streams_text_and_usage_and_sends_expected_request(self):
        seen = {}

        def handler(request):
            seen["headers"], seen["body"] = request.headers, json.loads(request.content)
            return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

        events = collect(AnthropicChatProvider("sk-test", transport=httpx.MockTransport(handler)))
        self.assertEqual("".join(e.text for e in events if isinstance(e, Delta)), "Hello world")
        done = events[-1]
        self.assertIsInstance(done, Done)
        self.assertEqual((done.usage.input_tokens, done.usage.output_tokens), (12, 5))
        self.assertEqual(seen["headers"]["x-api-key"], "sk-test")
        self.assertEqual(seen["body"]["model"], "m1")
        self.assertTrue(seen["body"]["stream"])
        self.assertEqual(seen["body"]["system"][0]["cache_control"], {"type": "ephemeral"})

    def _error(self, status, headers=None):
        p = AnthropicChatProvider("sk-secret-key", transport=httpx.MockTransport(
            lambda r: httpx.Response(status, headers=headers or {}, text="body with sk-secret-key and prompt hi")))
        with self.assertRaises(ProviderError) as cm:
            collect(p)
        self.assertNotIn("sk-secret-key", str(cm.exception))
        self.assertNotIn("prompt", str(cm.exception))
        return cm.exception

    def test_status_mapping(self):
        e = self._error(429, {"retry-after": "2"})
        self.assertEqual((e.kind, e.retry_after_s), ("rate_limit", 2.0))
        self.assertEqual(self._error(401).kind, "auth")
        self.assertEqual(self._error(400).kind, "bad_request")
        self.assertEqual(self._error(503).kind, "server")
        self.assertEqual(self._error(529).kind, "server")

    def test_connection_failure_and_repr_hide_the_key(self):
        def boom(request):
            raise httpx.ConnectError("no route")
        p = AnthropicChatProvider("sk-secret-key", transport=httpx.MockTransport(boom))
        with self.assertRaises(ProviderError) as cm:
            collect(p)
        self.assertEqual(cm.exception.kind, "connection")
        self.assertNotIn("sk-secret-key", repr(p))


if __name__ == "__main__":
    unittest.main()
