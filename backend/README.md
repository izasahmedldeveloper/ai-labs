# StudyBuddy backend

## Build status
| Slice | Design | Status |
|---|---|---|
| Chat orchestrator pipeline core (stages, context, required/optional rules, chunked output checks, idempotency, disconnect cancel, concurrency limits) | LLD 03 | **Done, 33 tests passing** (stdlib only, fake stages) |
| HTTP/SSE API (FastAPI): `POST /chat` as an SSE stream, gateway-header identity, enrollment check, 429 backpressure, keep-alives, disconnect cancel | LLD 03 §5, LLD 12 §4–5 | **Done, 27 tests** (60 total with the pipeline) |
| Real stages: guardrails, cache, router, retriever, model gateway | LLD 05–09 | Not started; each replaces a fake in `pipeline/fakes.py` |
| Data model + RLS migrations | LLD 01 | Not started |
| Auth (Keycloak JWT verification) | LLD 02 | Not started |
| Ingestion worker + Kafka | LLD 04 | Not started |

## Run the tests
```bash
cd backend
python3 -m unittest discover -s tests -v      # pipeline tests, no dependencies (API tests are skipped)
pip install -e ".[api,dev]" && pytest         # everything: pipeline + API
```

## Try the API locally
```bash
cd backend
uvicorn studybuddy.api.dev:app --reload       # real HTTP/SSE layer over the fake stages (dev only)
curl -N -X POST localhost:8000/chat -H 'content-type: application/json' \
  -H 'x-tenant-id: t1' -H 'x-user-id: u1' -H 'x-user-role: student' \
  -d '{"question":"What is normalization?","course_id":"c1","request_id":"demo-request-1"}'
```

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
| `studybuddy/api/dev.py` | Local demo wiring: the fakes behind the real API (allows every course: dev only) | — |
| `tests/test_api.py` | Auth, validation, enrollment, idempotent retry, 429, framing, disconnect-cancel | LLD 12 §10 |

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
