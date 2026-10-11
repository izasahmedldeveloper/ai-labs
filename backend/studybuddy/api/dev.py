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

from ..guardrails import RuleInputGuardrails, RuleOutputGuardrails
from ..pipeline import Identity, Orchestrator, PipelineConfig
from ..pipeline import fakes
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


def build_dev_retriever() -> HybridRetriever:
    embedder = HashingEmbedder(synonyms=DEMO_SYNONYMS)
    store = InMemoryChunkStore()
    chunks = [StoredChunk(f"t1-c1-{short}", text, "t1", "c1", doc, "v1", ordinal=ordinal, page=ordinal + 1,
                          heading="Course notes") for short, doc, ordinal, text in DEMO_NOTES]
    store.add(chunks, embedder.embed_sync([c.text for c in chunks]))
    for chunk in chunks:
        store.set_current(chunk.tenant_id, chunk.course_id, chunk.document_id, chunk.version_id)
    return HybridRetriever(store, embedder, reranker=OverlapReranker(DEMO_SYNONYMS))


def build_dev_orchestrator() -> Orchestrator:
    return Orchestrator(
        guardrails=RuleInputGuardrails(), cache=fakes.FakeCache(), router=fakes.FakeRouter(),
        retriever=build_dev_retriever(), generator=fakes.FakeGenerator(token_delay=0.05),
        output_guardrails=RuleOutputGuardrails(), escalation=fakes.FakeEscalation(),
        config=PipelineConfig(),
    )


def build_dev_app():
    return create_app(
        build_dev_orchestrator(),
        authenticate=GatewayHeaderAuth(os.environ.get("STUDYBUDDY_GATEWAY_SECRET") or None),
        check_enrollment=allow_all_enrollment,
    )


app = build_dev_app()
