"""Anthropic Messages API adapter (streaming). UNTESTED AGAINST THE LIVE API: it is tested only with
`httpx.MockTransport`. Error messages carry the status code / error type only, never the key, request or response body.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

from ..types import Delta, Done, ProviderError, ProviderRequest, Usage

API_URL = "https://api.anthropic.com/v1/messages"


def _status_error(status: int, headers) -> ProviderError:
    if status == 429:
        try:
            retry = float(headers.get("retry-after")) if headers.get("retry-after") else None
        except ValueError:
            retry = None
        return ProviderError("rate_limit", "http 429", retry)
    if status in (401, 403):
        return ProviderError("auth", f"http {status}")
    if status in (400, 404, 413, 422):
        return ProviderError("bad_request", f"http {status}")
    return ProviderError("server", f"http {status}")


class AnthropicChatProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, *, base_url: str = API_URL, transport=None, timeout_s: float = 30.0):
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self._url, self._transport, self._timeout = base_url, transport, timeout_s

    def __repr__(self) -> str:  # never print the key
        return "AnthropicChatProvider(<redacted>)"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[Delta | Done]:
        import httpx  # imported lazily so the core package works without it

        body = {"model": request.model, "max_tokens": request.max_output_tokens, "stream": True,
                "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": m.role, "content": m.content} for m in request.messages]}
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        tokens_in = tokens_out = 0
        stop = None
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                async with client.stream("POST", self._url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        raise _status_error(resp.status_code, resp.headers)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            data = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        kind = data.get("type")
                        if kind == "message_start":
                            tokens_in = int(data.get("message", {}).get("usage", {}).get("input_tokens", 0))
                        elif kind == "content_block_delta":
                            text = data.get("delta", {}).get("text")
                            if text:
                                yield Delta(text)
                        elif kind == "message_delta":
                            tokens_out = int(data.get("usage", {}).get("output_tokens", tokens_out))
                            stop = data.get("delta", {}).get("stop_reason", stop)
                        elif kind == "error":
                            etype = data.get("error", {}).get("type", "")
                            raise ProviderError("rate_limit" if etype == "rate_limit_error" else "server",
                                                f"stream error {etype}")
                        elif kind == "message_stop":
                            break
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("timeout", "request timed out") from None
        except httpx.TransportError:
            raise ProviderError("connection", "connection failed") from None
        yield Done(Usage(tokens_in, tokens_out), stop)
