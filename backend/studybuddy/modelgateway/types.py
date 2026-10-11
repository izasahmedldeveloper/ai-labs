"""Model Gateway contracts (LLD 09 Part B): requests, provider protocols, errors, routing table and configuration.

Application code asks for a LOGICAL model name ("chat-cheap", "chat-strong", "embed", "rerank"); the routing table maps
it to a provider and model. Swapping a provider is a config change plus an evaluation run, never a code change (B2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import AsyncIterator, Mapping, Protocol, Sequence


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (about 1.3 tokens per word), used for size limits and when a provider reports no usage."""
    return math.ceil(len(text.split()) * 1.3) if text.strip() else 0


# ----------------------------------------------------------------------------------------------- requests
@dataclass(frozen=True)
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ChatRequest:
    logical_model: str
    system: str
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int | None = None  # capped by the route's own maximum
    tenant_id: str = ""
    user_id: str = ""
    trace_id: str = ""
    lane: str = "live"  # "live" (students waiting) or "bulk" (ingestion, batch): separate capacity, B4


@dataclass(frozen=True)
class ProviderRequest:
    """What a provider adapter receives: an actual model name, never a logical one."""

    model: str
    system: str
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Done:
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


# ----------------------------------------------------------------------------------------------- errors
class ProviderError(Exception):
    """A provider call failed. `kind` drives retry, failover and the circuit breaker.

    The message must never contain credentials, prompts or model output (B5/B7): adapters build it from the
    status code and error type only.
    """

    RETRYABLE = frozenset({"rate_limit", "timeout", "server", "connection"})
    COUNTS_FOR_BREAKER = frozenset({"timeout", "server", "connection"})  # a 429 means "slow down", not "broken"

    def __init__(self, kind: str, message: str = "", retry_after_s: float | None = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind, self.retry_after_s = kind, retry_after_s

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE

    @property
    def counts_for_breaker(self) -> bool:
        return self.kind in self.COUNTS_FOR_BREAKER


class GatewayError(Exception):
    pass


class Busy(GatewayError):
    """Our own capacity or a provider rate limit would be exceeded: answer upstream with a clean 'busy'."""


class AllProvidersFailed(GatewayError):
    """Every provider for the logical model failed or is circuit-broken (degraded mode, B3)."""

    def __init__(self, kind: str):
        super().__init__(f"all providers failed (last error: {kind})")
        self.kind = kind


class StreamInterrupted(GatewayError):
    """The provider failed AFTER tokens were sent. Streams cannot be spliced across providers, so this is final."""


class RequestTooLarge(GatewayError):
    pass


class UnknownModel(GatewayError):
    pass


# ----------------------------------------------------------------------------------------------- providers
class ChatProvider(Protocol):
    name: str

    def stream(self, request: ProviderRequest) -> AsyncIterator[Delta | Done]: ...


class EmbedProvider(Protocol):
    name: str

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]: ...


class RerankProvider(Protocol):
    name: str

    async def rerank(self, model: str, query: str, texts: Sequence[str]) -> list[float]: ...


# ----------------------------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class Target:
    provider: str  # key into the gateway's provider registry
    model: str  # the provider's own model name


@dataclass(frozen=True)
class ModelRoute:
    logical: str
    primary: Target
    fallback: Target | None = None  # embeddings: None, the same model is required everywhere (LLD 04)
    kind: str = "chat"  # chat | embed | rerank
    ttft_timeout_s: float = 2.0  # first token: 2 s cheap / 3 s strong (B3)
    total_timeout_s: float = 30.0
    max_input_tokens: int = 8000
    max_output_tokens: int = 800


@dataclass(frozen=True)
class RoutingTable:
    version: str
    routes: Mapping[str, ModelRoute]

    def get(self, logical: str, kind: str) -> ModelRoute:
        route = self.routes.get(logical)
        if route is None or route.kind != kind:
            raise UnknownModel(f"no {kind} route named {logical!r}")
        return route


@dataclass(frozen=True)
class ProviderLimits:
    requests_per_minute: int
    burst: int = 10


@dataclass(frozen=True)
class GatewayConfig:
    live_slots: int = 64  # concurrent live calls
    bulk_slots: int = 8  # concurrent bulk calls: a flood of ingestion can never take live slots
    queue_wait_s: float = 0.5  # how long a call may wait for a slot or a provider rate-limit token before `Busy`
    retry_jitter_s: float = 0.1  # random delay before the single retry
    max_retry_wait_s: float = 1.0  # a longer Retry-After than this: fail over instead of waiting
    breaker_failures: int = 5
    breaker_window_s: float = 30.0
    breaker_cooloff_s: float = 60.0
    provider_limits: Mapping[str, ProviderLimits] = field(default_factory=dict)
    # model name -> (USD per million input tokens, USD per million output tokens). Deliberately empty by default:
    # prices are contract data, not something to guess. Without an entry, cost is recorded as None.
    prices: Mapping[str, tuple[float, float]] = field(default_factory=dict)
