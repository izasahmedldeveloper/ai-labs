"""Typed decisions and stage protocols (LLD 03 section 3).

The orchestrator only reads these decisions, never stage internals, so any stage
can be replaced (e.g. regex guardrail -> model-based) without touching the others.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Protocol

from .context import RequestContext


class GuardAction(str, Enum):
    ALLOW = "allow"
    REDACT = "allow_with_redactions"
    BLOCK = "block"


@dataclass(frozen=True)
class GuardrailDecision:
    action: GuardAction
    reason: str = ""
    text: str | None = None  # redacted text when action == REDACT


class Route(str, Enum):
    CACHE = "cache"
    RULE = "rule"
    REDIRECT = "redirect"
    CHEAP_LLM = "cheap_llm"
    STRONG_LLM = "strong_llm"
    HUMAN = "human"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    needs_retrieval: bool
    reason: str = ""
    canned_answer: str | None = None  # for RULE / REDIRECT; for HUMAN it replaces the default "passed to your tutor" text
    confidence: float | None = None  # LLD 07 s2; recorded, not used by the pipeline


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    document_id: str
    version_id: str
    page: int | None = None
    heading: str | None = None
    score: float = 0.0

    def source(self) -> dict:
        return {"chunk_id": self.id, "document_id": self.document_id, "version_id": self.version_id,
                "page": self.page, "heading": self.heading}


@dataclass(frozen=True)
class RetrievalResult:
    chunks: tuple[Chunk, ...] = ()
    confidence: str = "none"  # high | low | none

    @property
    def none_found(self) -> bool:
        return not self.chunks


@dataclass(frozen=True)
class CacheHit:
    answer: str
    sources: tuple[dict, ...] = ()


class ChunkAction(str, Enum):
    PASS = "pass"
    REDACT = "redact"
    BLOCK = "block"


@dataclass(frozen=True)
class ChunkDecision:
    action: ChunkAction
    text: str | None = None


@dataclass(frozen=True)
class Event:
    """SSE event sent to the browser: start | token | notice | ticket | done | error."""

    type: str
    data: dict = field(default_factory=dict)


class InputGuardrails(Protocol):
    def sanitize(self, text: str) -> str: ...  # fast rule layer (PII) before anything is embedded/logged
    async def check(self, ctx: RequestContext) -> GuardrailDecision: ...


class SemanticCache(Protocol):
    async def lookup(self, ctx: RequestContext) -> CacheHit | None: ...
    async def store(self, ctx: RequestContext, answer: str, sources: tuple[dict, ...]) -> None: ...


class Router(Protocol):
    async def route(self, ctx: RequestContext) -> RouteDecision: ...


class Retriever(Protocol):
    async def retrieve(self, ctx: RequestContext) -> RetrievalResult: ...


class Generator(Protocol):
    def stream(self, ctx: RequestContext, chunks: tuple[Chunk, ...]) -> AsyncIterator[str]: ...


class OutputGuardrails(Protocol):
    async def check_chunk(self, ctx: RequestContext, text: str, first: bool) -> ChunkDecision: ...
    async def final_ok(self, ctx: RequestContext, answer: str, chunks: tuple[Chunk, ...]) -> bool: ...


class Escalation(Protocol):
    async def create(self, ctx: RequestContext, reason: str) -> str: ...
