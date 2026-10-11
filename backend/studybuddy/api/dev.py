"""Local demo app: the real HTTP/SSE layer, real guardrails (LLD 08) and real hybrid retriever (LLD 05) on top of the
fake cache, router, model and escalation stages. The retriever searches a tiny seeded database course (tenant `t1`,
course `c1`); every other tenant/course has no notes, so it returns "not found".

    cd backend
    uvicorn studybuddy.api.dev:app --reload

    curl -N -X POST localhost:8000/chat \
      -H 'content-type: application/json' \
      -H 'x-tenant-id: t1' -H 'x-user-id: u1' -H 'x-user-role: student' \
      -d '{"question":"What is BCNF?","course_id":"c1","request_id":"demo-request-1"}'

Try "How do I cancel my course?" (found by meaning, not keywords), "What does CS-301 cover?" (exact term), and
"Who won the football world cup?" (not in the notes -> "I couldn't find this in your course notes").

DEV ONLY: every user counts as enrolled in every course, the notes live in memory, the embedder/reranker are
deterministic word-hashing fakes, and the model answers from canned text. Replace each as the real services
(LLD 01, 04, 06-09) are built.
"""
from __future__ import annotations

import os

from ..cache import InMemoryCacheStore, SemanticCache
from ..guardrails import Policy, RuleInputGuardrails, RuleOutputGuardrails
from ..guardrails.policy import ORDINAL_CITATION
from ..modelgateway import (BudgetTracker, GatewayGenerator, InMemoryUsageLedger, ModelGateway, PromptBuilder)
from ..modelgateway.providers.anthropic import AnthropicChatProvider
from ..modelgateway.providers.openai import OpenAIChatProvider
from ..modelgateway.types import ModelRoute, RoutingTable, Target
from ..pipeline import Identity, Orchestrator, PipelineConfig
from ..pipeline import fakes
from ..router import Router
from ..retrieval import HybridRetriever, InMemoryChunkStore, StoredChunk
from ..retrieval.fakes import HashingEmbedder, OverlapReranker
from .app import create_app
from .auth import GatewayHeaderAuth

DEMO_SYNONYMS = {"cancel": "drop", "course": "class", "withdraw": "drop", "withdrawal": "drop"}
DEMO_NOTES = (  # (short id, document, position, text)
    ("norm", "d1", 0, "Normalization removes redundancy from relational tables by splitting them. "
                      "It reduces update anomalies."),
    ("bcnf", "d1", 1, "BCNF is a stricter form of third normal form. Every determinant must be a candidate key."),
    ("drop", "d2", 0, "To drop a class, submit the withdrawal form before the deadline. "
                      "Withdrawal after week six needs approval."),
    ("cs301", "d2", 1, "CS-301 covers database systems. The prerequisite is CS-201 on data structures."),
    ("idx", "d3", 0, "An index speeds up lookups. A B-tree index keeps keys sorted."),
)


async def allow_all_enrollment(identity: Identity, course_id: str) -> bool:
    return True


def build_dev_retriever(embedder: HashingEmbedder | None = None) -> HybridRetriever:
    embedder = embedder or HashingEmbedder(synonyms=DEMO_SYNONYMS)
    store = InMemoryChunkStore()
    chunks = [StoredChunk(f"t1-c1-{short}", text, "t1", "c1", doc, "v1", ordinal=ordinal, page=ordinal + 1,
                          heading="Course notes") for short, doc, ordinal, text in DEMO_NOTES]
    store.add(chunks, embedder.embed_sync([c.text for c in chunks]))
    for chunk in chunks:
        store.set_current(chunk.tenant_id, chunk.course_id, chunk.document_id, chunk.version_id)
    retriever = HybridRetriever(store, embedder, reranker=OverlapReranker(DEMO_SYNONYMS))
    retriever.chunk_store = store  # the cache reads current document versions from here
    return retriever


class StoreVersions:
    """Current document versions straight from the (in-memory) chunk store; the real one reads Postgres (LLD 01)."""

    def __init__(self, store: InMemoryChunkStore):
        self._store = store

    async def current_version(self, tenant_id: str, course_id: str, document_id: str) -> str | None:
        return self._store.current_version(tenant_id, course_id, document_id)


def build_dev_gateway(openai_key: str | None, anthropic_key: str | None) -> ModelGateway:
    """Real gateway (LLD 09 B): OpenAI is the primary provider, Anthropic the fallback. Whichever key is missing is
    simply left out (one key = one provider, no failover). NOT TESTED AGAINST THE LIVE APIs. Model names are
    env-overridable DEFAULTS: check them against your accounts."""
    providers, order = {}, []
    if openai_key:
        providers["openai"] = OpenAIChatProvider(openai_key)
        order.append(("openai", os.environ.get("STUDYBUDDY_OPENAI_CHEAP_MODEL", "gpt-4o-mini"),
                      os.environ.get("STUDYBUDDY_OPENAI_STRONG_MODEL", "gpt-4o")))
    if anthropic_key:
        providers["anthropic"] = AnthropicChatProvider(anthropic_key)
        order.append(("anthropic", os.environ.get("STUDYBUDDY_ANTHROPIC_CHEAP_MODEL", "claude-haiku-5-5"),
                      os.environ.get("STUDYBUDDY_ANTHROPIC_STRONG_MODEL", "claude-sonnet-5-5")))
    routes = {}
    for logical, index, ttft in (("chat-cheap", 1, 2.0), ("chat-strong", 2, 3.0)):
        targets = [Target(entry[0], entry[index]) for entry in order]
        routes[logical] = ModelRoute(logical, targets[0], targets[1] if len(targets) > 1 else None, ttft_timeout_s=ttft)
    return ModelGateway(RoutingTable("dev-1", routes), providers, ledger=DEV_LEDGER)


DEV_LEDGER = InMemoryUsageLedger()
DEV_BUDGETS = BudgetTracker(DEV_LEDGER, tenant_monthly_budget={}, default_tenant_monthly=1_000_000)


def build_dev_orchestrator() -> Orchestrator:
    openai_key, anthropic_key = os.environ.get("OPENAI_API_KEY"), os.environ.get("ANTHROPIC_API_KEY")
    if openai_key or anthropic_key:
        # The pipeline waits longer for the first token than the gateway does, so failover can happen inside it.
        generator = GatewayGenerator(build_dev_gateway(openai_key, anthropic_key), PromptBuilder())
        config = PipelineConfig(model_ttft_timeout_s=8.0)
        output = RuleOutputGuardrails(Policy(citation_pattern=ORDINAL_CITATION, citation_ids="ordinal"))
    else:
        generator, config, output = fakes.FakeGenerator(token_delay=0.05), PipelineConfig(), RuleOutputGuardrails()
    embedder = HashingEmbedder(synonyms=DEMO_SYNONYMS)
    retriever = build_dev_retriever(embedder)
    guardrails = RuleInputGuardrails()
    cache = SemanticCache(InMemoryCacheStore(), embedder, StoreVersions(retriever.chunk_store),
                          policy_version=lambda: guardrails.policy_version)
    return Orchestrator(
        guardrails=guardrails, cache=cache, router=Router(embedder=embedder),
        retriever=retriever, generator=generator,
        output_guardrails=output, escalation=fakes.FakeEscalation(), config=config,
    )


def build_dev_app():
    return create_app(
        build_dev_orchestrator(),
        authenticate=GatewayHeaderAuth(os.environ.get("STUDYBUDDY_GATEWAY_SECRET") or None),
        check_enrollment=allow_all_enrollment,
        budget_state=DEV_BUDGETS.state,
    )


app = build_dev_app()
