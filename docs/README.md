# StudyBuddy design set — index

Enterprise, multi-tenant course-notes Q&A assistant (React + FastAPI, Keycloak, Postgres, Qdrant, Redis, Kafka, hosted LLMs).
Built to **learn production AI system design**: every concern from the codebasics video appears in at least one design.

## Reading order
1. `../notes/video-summary.md` — source notes · `../ROADMAP.md` — build stages · `stage-00-requirements.md` — requirements and capacity math
2. `01-high-level-design.md` — components, flows, ADRs
3. Low-level designs (`lld/`): 01 data model & tenancy → 02 auth & roles → 03 chat orchestrator pipeline → 04 ingestion & chunking → 05 retriever → 06 semantic cache + CDC → 07 router → 08 guardrails → 09 API gateway + model gateway → 10 escalation → 11 observability → 12 frontend & SSE

## Video concept → where it is designed
| Video concept | Design |
|---|---|
| Prototype vs production, HLD/LLD | HLD, this set |
| Requirements, capacity math | stage-00 |
| Authentication (OIDC/Keycloak) | LLD 02 |
| API gateway, rate limiting, token budgets | LLD 09 (Part A) |
| Input/output guardrails, PII, injection | LLD 08 (+ LLD 04 for poisoned documents) |
| Semantic cache | LLD 06 |
| CDC / freshness | LLD 06, LLD 04 |
| Minimum sufficient intelligence / hybrid routing | LLD 07 |
| Human-in-the-loop | LLD 10 |
| AI/model gateway, failover | LLD 09 (Part B) |
| Queuing / throttling (Kafka) | LLD 04, LLD 10, LLD 06 |
| Observability dashboards | LLD 11 |
| SSE streaming | LLD 03 §5, LLD 12 |
| RAG (extra) | LLD 04 + 05 |

## Terms used consistently
- **Tenant** = a university; **course** = unit of access and retrieval scope; one course per conversation.
- **Routes**: `cache`, `rule`, `redirect`, `cheap_llm`, `strong_llm`, `human` (LLD 07). Logical models: `chat-cheap`, `chat-strong`, `embed`, `rerank`, `judge` (LLD 09).
- **Required vs optional stage**: optional fails open, safety/correctness fails closed (LLD 03 §4).
- **Control Redis** (rate limits, idempotency, concurrency, denylist, sessions) vs **cache Redis** (semantic cache) — HLD ADR-8.
- **No self-hosted LLM.** Small classical classifiers run in our services; reranker, LLMs and embeddings are hosted.

## Shared numbers (single source — change here and in the cited LLD together)
| Item | Value | Where |
|---|---|---|
| Users / traffic | 2,000 students, ~4,800 req/day, peak ~1–3 QPS | stage-00 |
| Cost target | ≤ $50 / month overall (revisit after measurement) | stage-00, LLD 07 §8 |
| Availability | 99.5% | stage-00, LLD 11 |
| TTFT P95 / total P95 | < 1.5 s (≈1.6 s follow-ups) / < 6 s | LLD 03 §6, LLD 11 |
| Access token | 15 min + denylist for disabled users | LLD 02, LLD 12 |
| Chunk size / overlap | ~400 / ~50 tokens | LLD 04 |
| Retrieval k | 30+30 → 20 fused → 5 final; ≤ ~2,000 context tokens | LLD 05 |
| Cache threshold / TTL | cosine ≥ 0.92 / 7 days | LLD 06 |
| Upload limit | 50 MB, 300 pages, via pre-signed URL | LLD 04, LLD 09 |
| Old version cleanup / soft delete | 7 days / 30 days | LLD 04 |
| Raw chat retention (opt-in) | 30 days, separate table, assigned-tutor read only | LLD 01 |
| Rate limits | IP 60/min; user 20 chat/min (burst 5); 2 concurrent streams | LLD 09, LLD 03 |
| Escalation SLA | urgent 1 h, high 4 h, normal 1 business day | LLD 10 |

## Decisions made so far
Shared DB + `tenant_id` + Postgres RLS · one course per conversation · raw chat opt-in, ticket-only access · Kafka · no self-hosted LLM · Keycloak self-hosted, no university SSO in v1 · OCR deferred past v1 · hosted reranker · chunked output guardrails (streaming + safety).

## Still open (need your answer)
1. ~~Keycloak hosting~~ — **self-hosted** (decided). ~~University SSO~~ — **not in v1**; Google/email login, SSO-ready design (LLD 02).
2. **15-minute tokens + denylist** acceptable, or instant revocation everywhere? (LLD 02 D3) — default stays unless you object.
3. Confirm or change the **defaults** in each LLD's final decisions table (esp. LLD 04 D1 OCR, D6 soft-delete; LLD 06 D2 threshold; LLD 10 D2 SLAs).
4. Capacity numbers in stage-00 are assumptions — replace with your real expected users.

## Known limitations (accepted)
- Router decides before retrieval, so it can't judge difficulty from retrieved text (LLD 03/07).
- Chunked streaming can't retract already-sent chunks if the final groundedness pass fails (LLD 08).
- No mid-stream provider failover (LLD 09).
- No cross-course retrieval in v1 (LLD 01).
- Kafka is heavier than this scale needs (chosen for learning).
