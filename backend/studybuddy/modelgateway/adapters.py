"""Adapters that let the pipeline and retriever use the gateway through the interfaces they already know."""
from __future__ import annotations

from typing import AsyncIterator, Sequence

from ..pipeline import BudgetState, RequestContext
from .gateway import ModelGateway
from .prompt import PromptBuilder
from .types import ChatRequest, Delta

ROUTE_TO_MODEL = {"cheap_llm": "chat-cheap", "strong_llm": "chat-strong"}


class GatewayGenerator:
    """Pipeline `Generator`: yields text pieces. Soft budget limit halves the output cap (LLD 09 A4)."""

    def __init__(self, gateway: ModelGateway, prompts: PromptBuilder | None = None, *, max_output_tokens: int = 800):
        self.gateway, self.prompts, self.max_output_tokens = gateway, prompts or PromptBuilder(), max_output_tokens

    async def stream(self, ctx: RequestContext, chunks) -> AsyncIterator[str]:
        logical = ROUTE_TO_MODEL.get(ctx.route or "", "chat-cheap")
        cap = self.max_output_tokens
        if ctx.budget_state == BudgetState.SOFT_LIMIT:
            cap = max(1, cap // 2)
        req = ChatRequest(logical_model=logical, system=self.prompts.system(),
                          messages=self.prompts.messages(ctx.rewritten_query or ctx.question, chunks, ctx.history),
                          max_output_tokens=cap, tenant_id=ctx.identity.tenant_id, user_id=ctx.identity.user_id, trace_id=ctx.trace_id)

        def keep(record) -> None:
            ctx.model_call = record.summary()  # internal only; never part of ctx.usage (client-visible)

        async for event in self.gateway.stream_chat(req, on_record=keep):
            if isinstance(event, Delta) and event.text:
                yield event.text


class GatewayEmbedder:
    def __init__(self, gateway: ModelGateway, logical: str = "embed"):
        self.gateway, self.logical = gateway, logical

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.gateway.embed(self.logical, texts)


class GatewayReranker:
    def __init__(self, gateway: ModelGateway, logical: str = "rerank"):
        self.gateway, self.logical = gateway, logical

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        return await self.gateway.rerank(self.logical, query, texts)
