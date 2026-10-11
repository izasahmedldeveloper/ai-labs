# StudyBuddy backend

## Build status
| Slice | Design | Status |
|---|---|---|
| Chat orchestrator pipeline core (stages, context, required/optional rules, chunked output checks, idempotency, disconnect cancel, concurrency limits) | LLD 03 | **Done, 33 tests passing** (stdlib only, fake stages) |
| HTTP/SSE API (FastAPI): `POST /chat` as an SSE stream, gateway-header identity, enrollment check, 429 backpressure, keep-alives, disconnect cancel | LLD 03 §5, LLD 12 §4–5 | **Done, 27 tests** (60 total with the pipeline) |
| Real stage: **guardrails** (input limits, hidden-text and disguise-proof attack rules, PII and secret redaction, output PII/secret/leak checks, citation and groundedness checks, versioned policy with shadow mode) | LLD 08 | **Rule layer done, 42 tests.** Model-based layers (injection/policy classifier, NLI groundedness, sensitive-topic routing, repeat-offender counters) not built: see "Guardrail limits" below |
| Real stage: **retriever** (mandatory tenant+course filter, current-version only, hybrid vector + keyword search, RRF fusion, rerank with fail-open fallback, threshold/confidence, de-dupe, neighbour expansion, token budget, follow-up rewrite hook, shared embedding, evaluation harness) | LLD 05 | **Logic done, 45 tests**, running against an in-memory store and deterministic fakes. Real Qdrant, embedding model and reranker are not connected: see "Retriever limits" below |
| Real stage: **model gateway** (logical model names, routing table, retry + failover before first token only, circuit breaker, provider rate limit, live/bulk lanes, per-call records, usage ledger + budget states, prompt builder with escaped notes and `[S#]` labels, OpenAI primary + Anthropic fallback adapters) | LLD 09 B | **Logic done, 28 gateway tests + 9 adapter/wiring tests** against scripted providers and `httpx.MockTransport`. **Never run against the live API**: see "Model gateway limits" below |
| Real stage: **semantic cache** (tenant+course+embedding-model namespace, similarity threshold with a borderline second check, numbers/negations/names must agree, read-time version check, policy-version stamp, TTLs, exclusions, stampede marker, CDC consumer, evict/purge) | LLD 06 | **Logic done, 27 tests**, running against an in-memory store and the demo embedder. No Redis, Kafka or real embedding model: see "Semantic cache limits" below |
| Real stage: **router** (tier 1 rules with safety first, tier 2 intent classifier plug-in with a lexicon baseline and a trainable centroid model over the shared embedding, tier 3 complexity score, shadow mode, evaluation harness with sensitive-recall target) | LLD 07 | **Logic done, 30 tests**, rules and thresholds are placeholders, **no trained model, no real traffic**, **cascade not built**: see "Router limits" below |
| **Qdrant store** (`ChunkStore` on a real Qdrant: dense + BM25 sparse vectors, tenant/course/status/flagged filters, publish with no gap, idempotent writes, client-side filter backstop, current-version lookup for the cache) | LLD 05 D3, LLD 04 s4, LLD 01 s3 | **Logic done, 25 tests** on qdrant-client's in-process local mode. **Never run against a Qdrant server here** (none available): run the same tests with `QDRANT_URL` set. See "Qdrant store limits" |
| **Data model + row-level security** (shared DB, tenant_id on every table, composite FKs, UUIDv7, ENABLE+FORCE RLS, least-privilege roles, audited tutor read, append-only audit/messages, immutable document versions, migration runner, scoped-transaction access layer, repos wired into the API) | LLD 01 | **Done, 52 tests** against a real PostgreSQL as non-superuser roles. Needs Postgres to run them (skipped otherwise). See "Data model & RLS limits" |
| **Auth** (Bearer JWT verification RS256/ES256 only, JWKS cache with throttled refresh and stale-if-error, tenant only from the verified token, JIT user provisioning with least-privilege role, denylist, deny-by-default `authorize()`, role gate on `/chat`) | LLD 02 | **Done, 20 tests** with generated keys and mock key servers. **Never run against a real Keycloak**: see "Auth limits" |
| **Ingestion** (type sniffing from bytes, DOCX/PPTX/PDF/Markdown parsers, structure-aware chunker, injection scan with quarantine, embed with batching/backoff/content-hash reuse, idempotent worker, retry topics + dead-letter, outbox relay, atomic version swap, soft delete, 7/30-day cleanup) | LLD 04 | **Logic done, 43 tests** (31 without Postgres, 12 on real Postgres). **No real Kafka, object storage, malware scanner or OCR**: see "Ingestion limits" |
| **Escalation** (trigger table to priority and restricted flag, tickets with SLA, dedup and per-student rate limit, tutor queue with first-claim-wins, bounded audited conversation window, replies, student reopen/cancel, SLA monitor, notifier, FAQ promotion into the cache, safety text shown even if the ticket cannot be saved) | LLD 10 | **Logic done, 35 tests** on real Postgres. **No email/chat channel, no UI, no HTTP endpoints**: see "Escalation limits" |
| **HTTP routes + worker runner** (document upload/status/delete, student and tutor ticket endpoints, FAQ approval; `WorkerRunner` moving the outbox relay, ingestion consumer, notifier, SLA and cleanup passes with failures isolated) | LLD 04, 10, 12 | **Done, 4 end-to-end tests** over HTTP on real Postgres. Not wired into `dev.py`; no process entry point or deployment: see "Routes and runner limits" |
| **Observability** (metrics registry with Prometheus text output and windowed queries, PII-safe JSON logs, per-request telemetry with sampled traces, component event counters, SLO alert rules with multi-window burn rate, end-to-end canary with a cross-tenant check, eval regression gate, `/metrics` and tenant view) | LLD 11 | **Done, 32 tests**. No OpenTelemetry, Prometheus, Alertmanager or Langfuse: see "Observability limits" |

## Run the tests
Database tests (`tests/test_rls.py`, `test_db_repos.py`, `test_ingestion_pg.py`, `test_escalation*.py`) need PostgreSQL 14+ server binaries on the machine (a private throwaway cluster is started) or `TEST_DATABASE_URL` pointing at a database where the roles in `studybuddy/db/sql/000_bootstrap_roles.sql` exist. Without either they are skipped. Install `pip install -e .[db]`.

```bash
cd backend
python3 -m unittest discover -s tests -v      # pipeline + guardrails + retriever tests, no dependencies (API tests are skipped)
pip install -e ".[api,dev]" && pytest         # everything: pipeline + guardrails + retriever + API
```

## Try the API locally
```bash
cd backend
uvicorn studybuddy.api.dev:app --reload       # real API + guardrails + retriever over fake cache/router/model (dev only)
curl -N -X POST localhost:8000/chat -H 'content-type: application/json' \
  -H 'x-tenant-id: t1' -H 'x-user-id: u1' -H 'x-user-role: student' \
  -d '{"question":"What is BCNF?","course_id":"c1","request_id":"demo-request-1"}'
```
The demo course (`t1` / `c1`) has five tiny notes. Try "How do I cancel my course?" (found by meaning), "What does CS-301 cover?" (exact term) and "Who won the football world cup?" (not in the notes). The model is still canned text, so an answer unrelated to the retrieved note correctly gets the "could not be verified" notice.

## Code map (read in this order to learn the design)
| File | What it teaches | Design |
|---|---|---|
| `studybuddy/pipeline/context.py` | One shared request context; identity is read-only once set (a stage cannot change the tenant) | LLD 03 §2 |
| `studybuddy/pipeline/types.py` | Typed stage decisions and stage protocols: the orchestrator reads decisions, never stage internals | LLD 03 §3 |
| `studybuddy/pipeline/runner.py` | `run_stage`: timeout plus the rule *optional fails open, required fails closed* | LLD 03 §4 |
| `studybuddy/pipeline/orchestrator.py` | The pipeline: guardrails ‖ cache, router, retriever, model with one retry before the first token, chunked output checks, cache write after delivery | LLD 03 §1, §5, §6 |
| `studybuddy/pipeline/streams.py` | `StreamBuffer` (replay/reattach for duplicate request ids, abandon timer) and `ConcurrencyLimiter` (backpressure) | LLD 03 §7 |
| `studybuddy/pipeline/fakes.py` | Deterministic fake stages so tests are fast, free and repeatable | LLD 03 §8 |
| `tests/test_pipeline.py` | Executable version of the LLD 03 test table | LLD 03 §8 |
| `studybuddy/api/sse.py` | SSE wire format; keep-alive comments that never drop an event; closing the stream closes the pipeline | LLD 03 §5, §7; LLD 12 §4 |
| `studybuddy/api/auth.py` | Identity from trusted gateway headers only (never the body); optional shared secret; swap for a JWT verifier in LLD 02 | LLD 02, LLD 03 §2 |
| `studybuddy/api/app.py` | `create_app`: pre-stream problems are HTTP errors (401/403/422/429/503), in-stream problems are `error` events; enrollment is required and fails closed | LLD 03 §4, §7; LLD 12 §5 |
| `studybuddy/api/dev.py` | Local demo wiring: real API, guardrails and retriever over the other fakes (allows every course: dev only) | — |
| `tests/test_api.py` | Auth, validation, enrollment, idempotent retry, 429, framing, disconnect-cancel | LLD 12 §10 |
| `studybuddy/guardrails/text.py` | Two copies of the text: a meaning-preserving one that is forwarded, and a lossy "skeleton" used only to detect attacks (case, accents, look-alike letters, leetspeak, spacing tricks); PII/secret redaction | LLD 08 §3 |
| `studybuddy/guardrails/policy.py` | Versioned rules with `enforce / shadow / off` modes; narrow patterns so "ignore the rules of inference" is not blocked | LLD 08 §6 |
| `studybuddy/guardrails/input_guardrails.py` | `sanitize()` redacts before anything is cached, embedded or logged; `check()` decides; audit events carry rule ids and counts, never text | LLD 08 §3, §8 |
| `studybuddy/guardrails/output_guardrails.py` | Per-chunk secret/leak/PII checks, then a whole-answer pass (leaks across chunk boundaries, citations, groundedness) | LLD 08 §5 |
| `studybuddy/guardrails/classifiers.py` | The plug-in points for the classifier and NLI models, plus a lexical groundedness baseline | LLD 08 §2, §5 |
| `tests/test_guardrails.py` | Red-team set, benign set, PII, fail-closed, mid-stream block, shadow mode, latency, end-to-end with the orchestrator | LLD 08 §9 |
| `studybuddy/retrieval/types.py` | `SearchFilter` cannot exist without tenant and course; the store/embedder/reranker/rewriter contracts; tunable config (k values, thresholds, time budgets) | LLD 05 §2, §12; LLD 01 §3 |
| `studybuddy/retrieval/memory_store.py` | In-memory stand-in for Qdrant with real BM25 + cosine search, current-version-only and flagged-chunk exclusion | LLD 05 §4, §7; LLD 04 |
| `studybuddy/retrieval/selection.py` | Reciprocal rank fusion, threshold and confidence, de-duplication, token budget, "best first, second-best last" ordering | LLD 05 §4, §6 |
| `studybuddy/retrieval/retriever.py` | The stage: query prep, parallel searches, fuse, rerank, select; degrades (drop rerank, then one search path) before it fails; isolation tripwire behind the store's own filter | LLD 05 §2–§8 |
| `studybuddy/retrieval/evaluation.py` | Recall@5, MRR, Precision@5 and none-found accuracy, to run on every retrieval change | LLD 05 §9 |
| `studybuddy/retrieval/fakes.py` | Deterministic embedder, reranker and rewriter for tests and the demo (not quality benchmarks) | LLD 05 §10 |
| `tests/test_retrieval.py` | Filter enforcement, isolation, versions, hybrid, fusion, rerank fallback, none-found, budget, rewrite, evaluation baseline, end-to-end | LLD 05 §10 |
| `studybuddy/modelgateway/types.py` | Requests, provider protocols, error kinds (which retry, which trip the breaker), routing table, config | LLD 09 B1–B4 |
| `studybuddy/modelgateway/resilience.py` | Circuit breaker, provider token bucket, live/bulk lanes | LLD 09 B3, B4 |
| `studybuddy/modelgateway/gateway.py` | The gateway: size check, lanes, retry, failover before first token, `StreamInterrupted` after it, one `CallRecord` per call even when cancelled | LLD 09 B2–B5, B7 |
| `studybuddy/modelgateway/ledger.py` | `CallRecord` (no text), usage ledger, `BudgetTracker` (ok, warning 80%, soft, hard) | LLD 09 B7, A4 |
| `studybuddy/modelgateway/prompt.py` | System prompt first, notes escaped inside `<course_notes>` as data, `[S1]..[Sn]` labels matching `done.sources`, optional canary, question last | LLD 09 B6 |
| `studybuddy/modelgateway/adapters.py` | `GatewayGenerator` (route to model, soft limit halves output, call summary onto `ctx.model_call`), embedder and reranker adapters | LLD 03, 09 |
| `studybuddy/modelgateway/providers/openai.py`, `anthropic.py` | Streaming adapters (OpenAI Chat Completions, Anthropic native Messages API; no compatibility layer); error text never includes key, prompt or body | LLD 09 B5 |
| `tests/test_modelgateway.py`, `tests/test_openai_provider.py`, `tests/test_anthropic_provider.py` | Failover rules, breaker, rate limit, lanes, accounting, budgets, prompt escaping, citation labels, adapter | LLD 09 B |
| `studybuddy/cache/semantic_cache.py` | The stage: lookup (all acceptance rules in LLD 06 s3), store (exclusions, PII, TTLs), stampede marker, invalidation API | LLD 06 s3–s9 |
| `studybuddy/cache/text.py` | Question normalisation; personal / time-sensitive detection; number, negation and name signature | LLD 06 s2, s4 |
| `studybuddy/cache/types.py`, `memory_store.py` | Entry, config and the `CacheStore` contract (the Redis boundary); in-memory stand-in | LLD 06 s2, s13 |
| `studybuddy/cache/cdc.py` | `document.changed` consumer: idempotent entry deletion, rejects malformed events | LLD 06 s5 |
| `tests/test_cache.py` | Hit/miss, numbers/negation/names, isolation, freshness (event and read-time), policy version, failure, stampede, exclusions, streaming | LLD 06 s11 |
| `studybuddy/router/rules.py` | Tier 1: safety phrases first (supportive message plus ticket), then escalation phrases, greetings, thanks, help | LLD 07 s3, s6 |
| `studybuddy/router/classifiers.py` | Tier 2: lexicon baseline and trainable centroid classifier over the existing embedding | LLD 07 s4 |
| `studybuddy/router/complexity.py`, `router.py` | Tier 3 score (cheap vs strong), the stage with its failure fallbacks, and `ShadowRouter` | LLD 07 s5, s6, D6 |
| `studybuddy/router/evaluation.py` | Per-route precision/recall and the 98% sensitive-recall check | LLD 07 s9 |
| `studybuddy/db/sql/000_bootstrap_roles.sql`, `001_schema.sql`, `002_rls.sql` | Roles (none superuser/BYPASSRLS), tables with tenant_id and composite FKs, RLS policies, triggers, audited `tutor_read_ticket()` | LLD 01 |
| `studybuddy/db/migrate.py`, `session.py`, `repos.py` | Checksummed migrations; `Database.scoped()` sets tenant/user/role per transaction (fails closed); enrollment, conversation, audit and tutor repos | LLD 01 |
| `tests/pg_harness.py`, `tests/test_rls.py`, `tests/test_db_repos.py` | Private Postgres for tests; cross-tenant, privacy, role, audit, retention and migration tests | LLD 01 s3 |
| `studybuddy/api/routes.py`, `studybuddy/workers/runner.py`, `tests/test_routes_pg.py` | Thin HTTP layer over the services (404 for "not yours"), background loop with per-job failure isolation | LLD 04, 10, 12 |
| `studybuddy/observability/`, `tests/test_observability.py` | Metrics, scrubbing logger, request observer + sampler, event sink, alert engine, canary, eval gate, metrics routes | LLD 11 |
| `studybuddy/db/sql/004_escalation.sql` | Ticket states, tutor queue and claim policies, student/tutor update guards, append-only replies, bounded tutor window | LLD 10 s3, s4 |
| `studybuddy/escalation/policy.py`, `service.py` | Trigger table (reason -> priority, restricted), ticket creation with dedup/rate limit/advisory lock, student, tutor and resolve actions | LLD 10 s1-s5 |
| `studybuddy/escalation/sla.py`, `notifier.py` | Re-notify, instructor then admin escalation, one-time breach alert, auto-close; recipients by role and roster, no student text in notifications | LLD 10 s3, s7 |
| `studybuddy/escalation/faq.py` | Tutor approval -> `faq_entry` + cache entry (refuses personal data); rebuild from the table | LLD 10 s6 |
| `tests/test_escalation_rls.py`, `tests/test_escalation.py` | Queue, claim, student limits, replies, window, pipeline integration, lifecycle, notifications, SLA, FAQ, isolation | LLD 10 s9 |
| `studybuddy/ingestion/parsers.py`, `scan.py` | Real file type from bytes, size/page/zip-bomb/DTD limits, plain-language failure reasons; injection phrases flag a document and quarantine the chunk | LLD 04 s2 |
| `studybuddy/ingestion/chunker.py` | Heading-bounded chunks, ~400 tokens with ~50 overlap, tables/code whole, one chunk per slide, heading path embedded | LLD 04 s3 |
| `studybuddy/ingestion/worker.py`, `broker.py` | Idempotent job (deterministic chunk ids), retry topics 10 s/1 min/10 min, dead-letter, repair of the ready-but-not-live gap | LLD 04 s4, s5 |
| `studybuddy/ingestion/service.py`, `relay.py`, `cleanup.py`, `repo.py` | Upload transaction (version + outbox + audit), relay as `sb_relay`, replace/delete, cleanup jobs; Postgres repo as `sb_worker` | LLD 04 s5, s6 |
| `tests/test_ingestion.py`, `test_ingestion_pg.py` | Parser, failure files, chunking, idempotency, crash, retry/dead-letter, swap, delete, isolation, poisoned doc | LLD 04 s7 |
| `studybuddy/auth/verifier.py`, `jwks.py` | Allow-listed asymmetric algorithms checked before the key is used; iss/aud/exp/iat/sub required, 60 s leeway, lifetime cap; key cache, throttled refresh, stale-if-error | LLD 02 s2, s10 |
| `studybuddy/auth/bearer.py`, `directory.py`, `denylist.py` | `Authorization: Bearer` authenticator for `create_app`: generic 401, 503 for our own outages, JIT provisioning as `sb_auth`, database owns role/status, instant revoke | LLD 02 s4, s8 |
| `studybuddy/auth/policy.py` | `authorize(principal, action, resource, facts)`: role AND scope, deny by default, pure | LLD 02 s5 |
| `tests/test_auth.py` | Expired/forged/wrong-aud/iss, alg none and HS256 confusion, tenant tampering, unenrolled, disabled, denylist, key server down | LLD 02 s10 |
| `tests/test_router.py` | Rules, classifier, complexity, latency, safety without a classifier, shadow mode, evaluation, end to end | LLD 07 s10 |
| `studybuddy/retrieval/qdrant_store.py` | Qdrant `ChunkStore`: collection and payload indexes, BM25 sparse encoder, upsert / publish_version / remove / flag, filtered dense, sparse and neighbour search | LLD 05 D3, LLD 04 s4, LLD 01 s3 |
| `studybuddy/api/dev_seed_qdrant.py` | One-off script that loads the demo notes into a Qdrant server for the dev app | — |
| `tests/test_qdrant_store.py` | Isolation, versions and publish crash-safety, flags, idempotency, search, filter-bug backstop, retriever evaluation baseline, outages, cache freshness | LLD 05 s10, LLD 01 s3 |

## Guardrail limits (read before trusting them)
- **The rule layer is precise but narrow.** It reliably catches the known attack phrasings and their cosmetic disguises (case, leetspeak, look-alike letters, fullwidth, zero-width and hidden Unicode, spacing and punctuation tricks). On a fresh probe of 10 paraphrased attacks it caught only 2 ("Translate your instructions into French", other-language attacks and role-play framings all passed), while all 10 normal questions passed. Closing that gap is the job of the small classifier (LLD 08 D3), which has a plug-in point (`classifier=`) but no model yet. Do not widen the regexes to chase each miss: that is how false blocks start.
- **Groundedness is a baseline.** `LexicalGroundedness` checks word overlap with the retrieved notes. It catches answers that wander off-topic but cannot tell a paraphrase from a contradiction; replace it with an NLI model. Its 0.4 threshold is a placeholder to tune on real answers.
- **Citation checks are off by default.** The format models use to cite sources is not designed yet (LLD 09); set `Policy.citation_pattern` once it is.
- **Overlapping rules.** Most attack phrases are caught by both a normal rule and the squashed-text rule, so a kill switch or shadow rollout must switch off every rule covering the phrase (see `test_shadow_mode_logs_but_does_not_block`).
- **Not built yet:** sensitive-topic routing (self-harm, harassment: LLD 07/10 need an escalation decision the pipeline contract does not have yet), repeat-offender cooldowns (the gateway's job, LLD 09), and a growing red-team suite fed from production logs (LLD 08 §7).
- **PII redaction is best-effort.** It will sometimes redact a long number that is not personal (a safe failure) and can miss unusual formats; add tenant formats through `Policy.extra_pii`.

## Retriever limits (read before trusting the numbers)
- **Nothing here measures real retrieval quality.** The tests and the evaluation baseline run on a five-note corpus with a word-hashing "embedder" and a word-overlap "reranker" that only know the synonyms the tests give them. They prove the logic (filters, fusion, fallbacks, budgets, isolation); they say nothing about Recall@5 on real course notes. That needs the real embedding model, a real cross-encoder reranker and a per-course evaluation set of 50–200 questions (LLD 05 §9).
- **Thresholds are placeholders** (`RetrievalConfig.min_relevance` 0.30, `high_relevance` 0.60, from LLD 05 D5). They are calibrated to the fake reranker and must be re-tuned on the evaluation set whenever the reranker or embedding model changes.
- **Degraded search is capped at low confidence on purpose.** If the rerank is skipped (timeout, outage, or little time left), confidence is never `high`, so the answer is not cached. In that mode relevance comes from the vector similarity and word coverage of the query, which is coarser than a reranker.
- **Time budget.** The retriever stops at `total_budget_s` (0.28 s) so it finishes inside the orchestrator's 0.3 s retriever timeout, and it spends that time in this order: rewrite (follow-ups only), searches, rerank. A follow-up that uses the full rewrite budget will usually lose its rerank. LLD 03 §6 allows ~100 ms extra for follow-ups: raise both `PipelineConfig.retriever_timeout_s` and `RetrievalConfig.total_budget_s` by 0.1 if you enable the rewriter.
- **Conversation history is not wired.** `RequestContext.history` exists and the rewrite hook works, but nothing loads history yet (it comes from the conversation store, LLD 01, server-side only; the client is never trusted for it). Until then every question is treated as a first message.
- **Citations carry ids, page and heading but not the document title** (`Chunk` has no title field); the UI joins the title from `document_id`.
- **The in-memory store scans every chunk per query**, so it is for small corpora only. The Qdrant implementation of `ChunkStore` (payload indexes on tenant, course and version status) is the next piece of real infrastructure; it must pass the same tests, including the isolation and version tests.

## Model gateway limits (read before trusting it)
- **Never run against a live provider.** Both adapters are tested only with `httpx.MockTransport`. In `dev.py` OpenAI is the primary and Anthropic the fallback, enabled by `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (a missing key drops that provider; one key means no failover). Model names are env-overridable defaults (`STUDYBUDDY_OPENAI_CHEAP_MODEL` gpt-4o-mini, `..._STRONG_MODEL` gpt-4o, `STUDYBUDDY_ANTHROPIC_CHEAP_MODEL` claude-haiku-5-5, `..._STRONG_MODEL` claude-sonnet-5-5): check them and the wire formats against your accounts.
- **Prices are not guessed.** `GatewayConfig.prices` is empty, so `cost_usd` is `None` until you add contract prices. Budgets count tokens, not money.
- **Budget cut-offs are placeholders**: warning 80% (from LLD 09 A4), soft 90%, hard 100%. The ledger is in memory (Redis counters plus the Postgres ledger are not built), and counters can overshoot slightly under concurrency, which the design accepts.
- **Failover has only been proven with scripted fakes**, never between the two real providers. The same prompt behaves differently on each model: evaluate answers on both before trusting the fallback. Embedding and rerank adapters exist but no real embed/rerank provider does.
- **Timeouts interact.** The gateway's first-token wait (2 s cheap, 3 s strong) must be shorter than `PipelineConfig.model_ttft_timeout_s`, or the pipeline gives up before the gateway can fail over. `dev.py` raises it to 8 s when a real key is set. The orchestrator also retries once before the first token, so a provider outage can cost up to four attempts.
- **Oversized input is rejected, not trimmed** (`RequestTooLarge`), because trimming would break the `[S#]` to sources mapping. Nothing yet turns this into a friendlier answer; the pipeline treats it as a model failure.
- **Canary and citations.** Set the same canary in `PromptBuilder` and `Policy.canary`. Ordinal citation checking is on in dev only when a key is set (`Policy(citation_ids="ordinal")`); `require_citations` stays off until real answers show how often models forget labels.
- **Client-visible vs internal.** Model usage goes to `ctx.model_call`, never `ctx.usage` (which is sent in `done`).
- `create_app(budget_state=...)` takes the worse of the gateway header and the tracker.

## Semantic cache limits (read before trusting it)
- **The threshold is a placeholder.** 0.92 cosine (LLD 06 D2) and the 0.88 borderline floor mean nothing until measured with a real embedding model on labelled same/different-intent pairs from your traffic. Track false-hit rate (target below 1%) before turning this on for real students. The false-hit review sampling and thumbs-down wiring are not built (`evict` exists to call).
- **The second check is crude.** Numbers, negations and names must agree (`cache/text.py`), and borderline matches also need 50% content-word overlap (placeholder). It will miss subtle differences such as "inner" versus "outer" join. `SemanticCache(intent_check=...)` accepts a classifier.
- **Follow-ups bypass the cache** (both lookup and store), because the standalone rewrite only exists after retrieval starts. This is safe but loses hits (LLD 06 s3 wants the rewritten question).
- **Stampede protection is off by default** (`CacheConfig.stampede_wait_s = 0`). The pipeline caps the cache stage at 50 ms (`PipelineConfig.cache_timeout_s`), so waiting the 500 ms of LLD 06 D6 needs both raised, which slows every first-time question on that path. The test proves 50 identical questions make one model call when they are raised.
- **Personal / time-sensitive detection is a regex list** ("my grade", "today", "this week"...). It is a placeholder; real traffic will find gaps. Deadline-like content gets a 1-day TTL instead of 7.
- **Policy version must be wired.** Pass `policy_version=lambda: guardrails.policy_version`; if it does not match what the pipeline stamps, nothing is ever served (safe, but silently useless).
- **Not built:** Redis/RediSearch store, Kafka consumer loop (only `CdcConsumer.handle`), per-user delete purge, hit-rate / $-saved dashboards (`cache.stats` counters and `ctx.cache_trace` exist), embedding-model rollover (new `embedder_id` = new namespace; old entries age out).

## Router limits (read before trusting it)
- **Nothing here is trained or measured on real students.** The phrase lists in `router/rules.py` and `classifiers.py` and the complexity weights in `complexity.py` are placeholders. The evaluation set in the tests is 15 synthetic questions I wrote, so a 100% score proves the harness works and nothing else. The 98% sensitive-recall target (LLD 07 D5) can only be checked on a labelled set built from real or realistic questions, reviewed by someone who knows the course and student-welfare context.
- **Safety rules are a first line, not a safety system.** They are plain phrase matches: they miss paraphrases (my own first test caught "thinking of ending it" slipping through) and they over-trigger (a psychology question that quotes "suicide" goes to a person). A match creates a ticket and shows `RouterConfig.safety_reply`, a generic supportive text with no phone numbers. That text, the crisis resources for your region and what staff do with those tickets need review by someone qualified before real students use this.
- **The cascade (LLD 07 s5, D4) is not built.** Retrying a failed cheap answer with the strong model conflicts with LLD 03's streaming design: tokens reach the student before the final groundedness check, so there is nothing to retry silently. Options: hold back the first N tokens until a quick check passes, or stream a cheap answer and offer "try a deeper answer". Until chosen, `ctx.router_trace["cheap_failed_final_check"]` marks the under-routing cases so the rate (target under 10%) can be measured.
- **Deviations from LLD 07 s3:** a bare one-word message is not auto-handled by a rule ("BCNF" is a valid question); the "office hours" lookup template needs course data that does not exist yet.
- **Redirect threshold** (0.60) and **sensitive threshold** (0.35, deliberately low) are placeholders. The centroid classifier has only been fitted on 4 toy sentences with the demo embedder.
- **Shadow mode** (`ShadowRouter`) compares decisions in-process; there is no store or dashboard for the comparisons yet. Routing decisions are in `ctx.router_trace` (internal, never sent to the client); cost per route needs the ledger and observability (LLD 11).

## Qdrant store limits (read before trusting it)
- **Never run against a real Qdrant server in this build.** The sandbox had no server and no Docker daemon, so every test uses qdrant-client's in-process local mode. Local mode ignores payload indexes (so index creation and speed are unproven) and its scoring can differ from a server. Before relying on it: `docker run -p 6333:6333 qdrant/qdrant`, then `QDRANT_URL=http://localhost:6333 python3 -m unittest tests.test_qdrant_store` (the same tests, each in its own throw-away collection; two of them need the fault-injecting local client and skip on a server).
- **Visibility is a `status` payload field**, not an atomic swap. `publish_version` marks the new version `current` first and the old one `superseded` second, so a crash leaves both visible (the retriever de-duplicates) and never neither; re-running the publish finishes the job (tested). A version with no chunks cannot be published.
- **Keyword search is BM25 via sparse vectors** (LLD 05 D3). Terms are hashed to 31-bit ids (collisions possible, about 1 in 2 billion per pair), the IDF is computed by Qdrant, and `Bm25SparseEncoder.avg_doc_len` (250 words) is a placeholder: set it from real chunks. Re-tune if you change the tokenizer: it is shared with the in-memory store.
- **Two filters, deliberately.** The server filters on tenant, course, `status == current` and not `flagged`; the client re-checks every returned point and drops and reports (`on_event`, `store.violations`) anything that should not be there. Dropping rather than raising is intentional because the retriever treats search exceptions as outages and would hide the bug. Wire `on_event` to an alert.
- **Point ids are uuid5(tenant, course, chunk id)**, so a chunk id reused by another tenant cannot overwrite your data. Changing this scheme means re-indexing.
- **Collections are per embedding model** (`chunks_v{N}`); a dimension mismatch is an error. `switch_alias` exists for the zero-downtime swap but the rebuild job (re-embed from Postgres) is part of ingestion and is not built.
- **Not built:** the Postgres `chunk` table that is the source of truth, the ingestion worker that calls `upsert` / `publish_version`, shard or replica settings, authentication beyond passing `api_key`, and backpressure or retry on Qdrant errors (the retriever's timeouts and fail-open paths are the only protection).
- **Dev app:** with `QDRANT_URL` set it searches Qdrant instead of the in-memory notes; seed once with `python -m studybuddy.api.dev_seed_qdrant`. `qdrant-client` is in the `qdrant` extra.

## Things worth experimenting with (learning exercises)
1. In `fakes.py`, make `FakeGuardrails(fail=True)` and watch the request stop with `input_guardrails_error`. Then flip that stage to optional in `orchestrator.py` and see which test fails: that failing test is the reason safety stages must fail closed.
2. Raise `FakeGenerator(ttft_delay=...)` above `model_ttft_timeout_s` and read the `model_ttft` timing entries on the context.
3. Change `PipelineConfig.disconnect_grace_s` from `0.0` to `2.0` and reconnect with the same `request_id` inside the grace period: the stream reattaches instead of restarting.
4. Add a second tenant to the cache test and try to make it leak; the cache key includes tenant and course, so it cannot.

## Notes for the next slices
- `create_app` requires a `check_enrollment(identity, course_id)` callable on purpose; wire the real one when LLD 01 exists. `dev.py` allows everything.
- Identity comes from gateway headers (`x-tenant-id`, `x-user-id`, `x-user-role`, `x-budget-state`, optional `x-gateway-secret`). When LLD 02 lands, pass a JWT-verifying `authenticate=` instead; nothing else changes.
- Idempotency and concurrency state is in memory (single process). Move it to Redis (LLD 03 §7) before running more than one API instance.
- The API tests need `pip install -e ".[api,dev]"` (network access to PyPI is now available in this environment).

## Data model & RLS limits
- Tests need a real PostgreSQL. The harness uses trust auth on a unix socket: test-only, never a deployment pattern. Role passwords are set via `bootstrap(conn, passwords=...)` and must come from your secret store.
- RLS protects against missing filters, not against code that holds a connection: such code can still run `SET app.tenant_id`. Keep raw connections inside `Database.scoped()` and review any new use of the pool.
- Instructors have no roster access and there are no analytics views yet. `message`, `audit_log`, `usage_ledger` are not partitioned. There is no relay role for the outbox (a cross-tenant scan needs its own reviewed design). The usage ledger is not yet persisted to Postgres. Retention/purge jobs are not built.

## Auth limits
- Never run against a real Keycloak. Realm, client, `tenant_id` and `roles` claim mappers, audience and token lifetime are configuration you must create; none is provided here. Claim names are assumptions from LLD 02 s4.
- The database owns role and status after first login. Token roles only choose the starting role of a new user (never `tenant_admin`); changing IdP groups later does not change an existing user's role.
- Only `/chat` is gated by `authorize()` (role gate; enrollment stays in the repo). Document, analytics, ticket and admin endpoints do not exist yet; their rules are in the policy table and tested, not enforced anywhere.
- Denylist is in memory (single process); a Redis one is not built. If the denylist errors, requests fail closed with 503. Nothing yet writes to it when an admin disables a user.
- Not built: refresh-token handling (IdP/SPA side), service identities for workers (client-credentials), gateway-level route rules, login/role-change audit rows, break-glass access.

## Ingestion limits
- Kafka is not used: `Broker` is a small contract with an in-memory implementation. Object storage is in memory too. A real adapter for each, and the process that runs `IngestionConsumer` and `OutboxRelay` on a loop, are not built.
- The malware scanner is `AcceptAllScanner`, which scans nothing. Parsing runs in a thread with a timeout, not in a sandboxed process, so a parser bug or a hang is not contained. Both must change before real uploads.
- OCR is deferred (D1): scanned PDFs fail with a clear reason. PDF headings are guessed from short lines (PDFs carry no structure) and tables inside PDFs are plain text. DOCX/PPTX are read with the standard library, so text boxes and embedded objects are partly missed. Not tested on real course files.
- Retries: one first try plus three retries (10 s, 1 min, 10 min), then dead-letter. Reading D4 as "3 attempts" would drop the last delay. Nothing yet reads the dead-letter topic or sends the alert beyond a callback.
- The embedding cache that skips unchanged chunks is in memory, so after a restart a new version is fully re-embedded. Embedding-model switching (versioned collections and alias flip) exists in the Qdrant store but no job drives it.
- Cleanup is given tenant ids (no role lists all tenants), nothing schedules it, and old-version cleanup only removes chunks. Orphan objects from a failed upload commit are deleted on the spot but a crash between storing and committing leaves one.
- The relay is at least once and publishes cross-tenant rows through the `sb_relay` role, which can read the outbox of every tenant by design; it should only ever run in the trusted worker tier. Only `/chat` has an HTTP route; the upload, status and delete endpoints are service methods with no API in front of them yet.
- The tutor-visible "document flagged for review" is only a column and an audit row; there is no review screen or release action.

## Escalation limits
- Triggers wired into the chat pipeline: router (safety, sensitive topics) and two misses in a row. The explicit "Ask a tutor" button, failed answer checks, and "all model providers down" are supported by `TicketService.request_tutor` / `PgEscalation.create(reason)` but nothing calls them yet; frustration detection is deferred (D6). There are no HTTP endpoints for tickets, only service methods.
- The priority table, restricted topics, SLAs (urgent 1 h, high 4 h, normal 24 h) and re-notify intervals are placeholders to agree with the tutoring team. SLAs are wall-clock hours; business hours and per-tenant overrides are not modelled.
- Notifications go through a channel interface with an in-memory implementation. No email, in-app or Slack/Teams channel exists, and user ids are not mapped to addresses anywhere (the database holds no emails). The tutor "queue" is a query, with no UI. The on-call list is configuration only; the safety "drills" in LLD 10 s7 are not built.
- The student gets an immediate message because the ticket is written in Postgres before the stream ends; the event is relayed later. If Postgres itself is down, a safety message still shows with its resources plus a notice that no tutor was alerted; any other escalation fails with an honest error. Nothing alerts operators about `ticket_failed` yet beyond a field in the router trace.
- A student sees `student_id` of tickets only as the tutor queue's row; the queue shows metadata, never the question. Tutors can see who asked (an id, not a name).
- Student text in replies and notes is redacted with the same rules as chat, which only catches the patterns the guardrails know (this work widened phone numbers to cover UK-style numbers). It is not a guarantee against personal data in free text.
- One rolling conversation per course is assumed (LLD 01 D1). The "repeated miss" count looks at the last 10 assistant messages of that conversation.
- Approved FAQ answers are stamped with the guardrail policy version like any cache entry, so a policy change hides them until `FaqSync.reload` runs; nothing runs it automatically. "Retrieval gap" and "bot could have answered" are stored, but no report reads them.
- Tickets the student cancels are closed but their tutor is not told beyond the event; reassignment of a stuck ticket by an admin works by SQL rule but has no service method.

## Routes and runner limits
- `add_routes` is not called by `api/dev.py` and there is no `main` that builds the databases, broker, object store and runner from environment settings. Wiring is shown only in `tests/test_routes_pg.py`.
- Uploads are read into memory (capped at 50 MB) rather than streamed to storage; the file goes in as the raw request body with an `X-Filename` header, not multipart. There is no antivirus step and no per-user upload rate limit.
- Route paths and payloads are my own design; LLD 12 has not been checked against a real frontend. No endpoint returns a student's ticket list to an instructor, and there are no admin routes (users, enrollments, assignment).
- `WorkerRunner` runs everything in one loop. Per-tenant jobs get tenant ids from a callback, because no role can list tenants. Nothing drives it in production, and the broker is still in memory, so events do not survive a restart: the outbox keeps unpublished rows, but rows already moved to the in-memory broker and not yet handled are lost.

## Observability limits
- Everything is in-process and dependency-free. There is no OpenTelemetry SDK, Prometheus server, Alertmanager, Grafana or Langfuse: `Registry.render()` produces scrape-ready text, `Trace`/`SpanExporter` is the seam for a real exporter, and `AlertEngine(notify=...)` is the seam for paging. Nothing calls `AlertEngine.evaluate()` on a timer yet; add it to `WorkerRunner`.
- Windowed queries keep the last 20,000 samples per metric in memory, so on a busy node the window is shorter than asked, and everything resets on restart. Real alerting should run on Prometheus.
- Tenant ids are a metric label. That is fine for tens or hundreds of tenants, not for tens of thousands.
- Alert thresholds, the 10% sample rate and the burn-rate factor come from the LLD or are my placeholders. Unmeasured: spend baseline is the average of the previous three hours, which is crude.
- Not wired in: `create_app`/`dev.py` do not install `RequestObserver` or `add_metrics_routes`, and no component is given the `EventSink` by default. `sb_pii_leaks_total` and `sb_urgent_tickets_unclaimed` exist but nothing sets them yet (a log scanner and a queue-depth job are needed). The canary needs two seeded test tenants and a way to ask through the real pipeline; only the checker is built.
- Logs refuse a fixed list of text-bearing keys and scrub strings with the same PII patterns as the guardrails, so PII in an unusual format can still get through in an allowed field.
- The eval gate compares numbers; producing them (running the eval set) is not built.
