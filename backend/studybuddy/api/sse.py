"""SSE wire format and streaming helpers (LLD 03 section 5, LLD 12 section 4).

Events are `start -> token* -> done | error` (plus optional `notice` / `ticket`). A comment line is sent
periodically so proxies and the browser can tell a quiet stream from a dead one (the client treats more
than 30 s of silence as a dropped connection, so the default interval here is half of that).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from ..pipeline import Event

KEEPALIVE_COMMENT = ": keep-alive\n\n"
DEFAULT_KEEPALIVE_S = 15.0


def encode_event(event: Event) -> str:
    """One SSE frame. json.dumps escapes newlines, so `data:` is always a single line."""
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.type}\ndata: {data}\n\n"


async def aclose_quietly(iterator) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:
            pass


async def sse_frames(first: Event | None, rest: AsyncIterator[Event],
                     keepalive_s: float = DEFAULT_KEEPALIVE_S) -> AsyncIterator[str]:
    """Turn pipeline events into SSE text, inserting keep-alive comments while the pipeline is quiet.

    `first` is an event already pulled from `rest` (the route handler peeks at it to choose the HTTP
    status). If the consumer goes away, this generator is closed, which closes `rest`, which lets the
    orchestrator notice the disconnect and cancel the model call (LLD 03 section 7).
    """
    pending: asyncio.Future | None = None
    try:
        if first is not None:
            yield encode_event(first)
        while True:
            if pending is None:
                pending = asyncio.ensure_future(rest.__anext__())
            # asyncio.wait (not wait_for): a timeout must NOT cancel the pending read.
            done, _ = await asyncio.wait({pending}, timeout=keepalive_s)
            if not done:
                yield KEEPALIVE_COMMENT
                continue
            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                return
            yield encode_event(event)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await aclose_quietly(rest)
