# AI System Design Tutorial (codebasics) — Notes

Summary of the codebasics video on taking an AI app from prototype to production. Timestamps are in `[mm:ss]`.

## 1. Prototype vs. Production AI System Design
- **Prototype problem:** React + FastAPI + LangChain + OpenAI works in dev but lacks rate limiting, guardrails and load balancing [01:31].
- **High-Level Design (HLD):** overall architecture and components — gateways, caching layers, routers, vector stores, models [03:00].
- **Low-Level Design (LLD):** chunking strategies, class definitions, replica topology, prompt management [03:18].

## 2. Requirements Gathering & Capacity Estimation
- **Functional** (track orders, manage refunds) vs. **non-functional** (P95 latency < 3s, 99.9% uptime, cost ceilings, zero PII leaks) [03:51].
- **Back-of-the-envelope math:** requests/day, peak QPS, monthly token consumption [04:45].

## 3. Authentication, Rate Limiting & Security
- **Auth:** OIDC/OAuth (e.g. Keycloak) for role-based access and SSO [06:32].
- **API gateway & rate limiting:** Cloudflare, NGINX or cloud firewalls; DoS defense, per-user request limits and token budgets [09:40].
- **Guardrails (input/output):** block prompt injection (e.g. tricking a bot into unintended discounts), sanitize PII via regex, specialized models, or LLM-as-a-judge [11:47].

## 4. Cost & Performance Optimization
- **Semantic caching:** serve common, non-dynamic questions (FAQs) from Redis or a vector cache to avoid redundant LLM calls [14:28].
- **CDC & freshness:** change-data-capture events invalidate stale cache entries and trigger targeted re-indexing when policies/documents change [19:42].
- **Minimum sufficient intelligence / hybrid routing:**
  - Deterministic rules / regex for trivial greetings and extractions [23:41].
  - Classical ML / fast classifiers (e.g. XGBoost) for intent classification [24:38].
  - Smaller/open LLMs (e.g. Mixtral) for simple tasks; frontier models only for complex reasoning [26:05].

## 5. High-Stakes Handling & Resilience
- **Human-in-the-loop:** auto-escalate emotionally charged or high-stakes queries (cancellations, billing disputes) [21:51].
- **AI/model gateway:** proxies like LiteLLM give provider failover (e.g. during an OpenAI outage), credential security and central quota management [32:16].
- **Queuing & throttling:** Kafka/RabbitMQ absorb traffic spikes, avoid model rate-limit errors and handle async long-running agent tasks [34:37].

## 6. Observability & Serving
- **Telemetry dashboards:** Langfuse, DataDog, New Relic or MLflow tracking P95 latency, spend and tool-call evals, with alerting [36:29].
- **Streaming:** token-by-token output over Server-Sent Events (SSE) to minimize time-to-first-token [41:45].
