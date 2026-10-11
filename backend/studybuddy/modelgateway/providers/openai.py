"""OpenAI Chat Completions adapter (streaming). UNTESTED AGAINST THE LIVE API: tested only with `httpx.MockTransport`.
Error messages carry the status code / error type only, never the key, request or response body.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

from ..types import Delta, Done, ProviderError, ProviderRequest, Usage
from ._http import status_error

API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIChatProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, *, base_url: str = API_URL, transport=None, timeout_s: float = 30.0):
        self._key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        self._url, self._transport, self._timeout = base_url, transport, timeout_s

    def __repr__(self) -> str:  # never print the key
        return "OpenAIChatProvider(<redacted>)"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[Delta | Done]:
        import httpx  # imported lazily so the core package works without it

        body = {"model": request.model, "stream": True, "stream_options": {"include_usage": True},
                "max_completion_tokens": request.max_output_tokens,
                "messages": [{"role": "system", "content": request.system},
                             *({"role": m.role, "content": m.content} for m in request.messages)]}
        headers = {"authorization": f"Bearer {self._key}", "content-type": "application/json"}
        tokens_in = tokens_out = 0
        stop = None
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                async with client.stream("POST", self._url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        raise status_error(resp.status_code, resp.headers)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            data = json.loads(payload)
                        except ValueError:
                            continue
                        if data.get("error"):
                            raise ProviderError("server", f"stream error {data['error'].get('type', '')}")
                        for choice in data.get("choices") or []:
                            text = (choice.get("delta") or {}).get("content")
                            if text:
                                yield Delta(text)
                            stop = choice.get("finish_reason") or stop
                        usage = data.get("usage")
                        if usage:
                            tokens_in = int(usage.get("prompt_tokens", tokens_in))
                            tokens_out = int(usage.get("completion_tokens", tokens_out))
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("timeout", "request timed out") from None
        except httpx.TransportError:
            raise ProviderError("connection", "connection failed") from None
        yield Done(Usage(tokens_in, tokens_out), stop)
