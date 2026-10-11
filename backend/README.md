# StudyBuddy backend

## Build status
| Slice | Design | Status |
|---|---|---|
| Chat orchestrator pipeline core (stages, context, required/optional rules, chunked output checks, idempotency, disconnect cancel, concurrency limits) | LLD 03 | **Done, 33 tests passing** (stdlib only, fake stages) |
| HTTP/SSE API (FastAPI) | LLD 03 §5, LLD 12 | Next: needs `fastapi` (see "Blocked" below) |
| Real stages: guardrails, cache, router, retriever, model gateway | LLD 05–09 | Not started; each replaces a fake in `pipeline/fakes.py` |
| Data model + RLS migrations | LLD 01 | Not started |
| Auth (Keycloak JWT verification) | LLD 02 | Not started |
| Ingestion worker + Kafka | LLD 04 | Not started |

## Run the tests
```bash
cd backend
python3 -m unittest discover -s tests -v      # no dependencies needed
# or, once pytest is installed:  pytest
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

## Things worth experimenting with (learning exercises)
1. In `fakes.py`, make `FakeGuardrails(fail=True)` and watch the request stop with `input_guardrails_error`. Then flip that stage to optional in `orchestrator.py` and see which test fails: that failing test is the reason safety stages must fail closed.
2. Raise `FakeGenerator(ttft_delay=...)` above `model_ttft_timeout_s` and read the `model_ttft` timing entries on the context.
3. Change `PipelineConfig.disconnect_grace_s` from `0.0` to `2.0` and reconnect with the same `request_id` inside the grace period: the stream reattaches instead of restarting.
4. Add a second tenant to the cache test and try to make it leak; the cache key includes tenant and course, so it cannot.

## Blocked in this environment
`pypi.org` is denied by the session's network policy (proxy 403), so `fastapi`, `pytest` and `uvicorn[standard]` extras cannot be installed here. To unblock: edit the cloud environment's **Network access** (allow `pypi.org` and `files.pythonhosted.org`, or tick *Allow package managers*), then start a new session or retry `pip install -e ".[api,dev]"`.
