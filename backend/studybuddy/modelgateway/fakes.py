"""Scripted provider for tests and the offline demo. No network."""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator, Sequence

from .types import Delta, Done, ProviderError, ProviderRequest, Usage


class ScriptedProvider:
    """Each call takes the next script step. A step is a dict:
    `error` (ProviderError raised before any token), `delay` (seconds before the first token), `text`,
    `fail_after` (raise `ProviderError('server')` after that many pieces), `token_delay`, `usage`.
    When the script runs out the last step repeats."""

    def __init__(self, name: str = "fake", script: Sequence[dict] | None = None):
        self.name = name
        self.script = list(script or [{"text": "Hello from the fake model [S1]."}])
        self.calls = 0
        self.requests: list[ProviderRequest] = []
        self.closed = 0

    def _step(self) -> dict:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step

    async def stream(self, request: ProviderRequest) -> AsyncIterator[Delta | Done]:
        self.requests.append(request)
        step = self._step()
        try:
            if step.get("error") is not None:
                raise step["error"]
            if step.get("delay"):
                await asyncio.sleep(step["delay"])
            pieces = re.findall(r"\S+\s*", step.get("text", ""))
            for i, piece in enumerate(pieces):
                if step.get("fail_after") is not None and i == step["fail_after"]:
                    raise ProviderError("server", "connection reset")
                if step.get("token_delay"):
                    await asyncio.sleep(step["token_delay"])
                yield Delta(piece)
            yield Done(step.get("usage", Usage(10, len(pieces))), "end_turn")
        finally:
            self.closed += 1

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        step = self._step()
        if step.get("error") is not None:
            raise step["error"]
        return [[float(len(t)), 1.0] for t in texts]

    async def rerank(self, model: str, query: str, texts: Sequence[str]) -> list[float]:
        step = self._step()
        if step.get("error") is not None:
            raise step["error"]
        return [1.0 / (1 + i) for i, _ in enumerate(texts)]
