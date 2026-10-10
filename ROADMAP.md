# StudyBuddy — build roadmap (learn system design by building)

**Goal:** a course-notes Q&A assistant, built stage by stage. Each stage adds ONE production
concern from the video, with a design doc (`docs/`) written *before* the code:
the problem, options considered, the trade-off we chose, and how to verify it.

**How we work:** Claude implements; you own the design. Every stage ends with
"Decisions for you" questions in its doc — answer them (even briefly) before moving on.

| Stage | Concern | Core design question |
|---|---|---|
| 0 | Requirements & capacity math | What does "production-ready" mean *for this app*? |
| 1 | Naive prototype (RAG) | What breaks first? (baseline to compare against) |
| 2 | Auth & roles | Who is allowed to ask / upload what? |
| 3 | API gateway, rate limits, token budgets | How do we survive abuse and cost blowups? |
| 4 | Input/output guardrails | Prompt injection, PII, off-topic |
| 5 | Semantic cache | When is a cached answer "the same question"? |
| 6 | Freshness / CDC re-indexing | How do cache + index learn notes changed? |
| 7 | Hybrid routing (rules → classifier → cheap hosted LLM → strong hosted LLM) | Minimum sufficient intelligence |
| 8 | Human-in-the-loop escalation | What is too risky/uncertain for the bot? |
| 9 | Model gateway & failover (LiteLLM-style) | What if a provider is down? |
| 10 | Queues & throttling | Spikes and long-running jobs (e.g. ingest big PDFs) |
| 11 | Observability | P95, cost, quality — how do we know it works? |
| 12 | SSE streaming | Time-to-first-token UX |

Stages 2–12 can be reordered after Stage 1; tell me if you want a different order.
