# LLD 01 — Data model & tenancy

Why first: every component reads/writes these entities, and tenant isolation is the one mistake
we cannot afford. Assumes HLD ADR-2 (shared DB, `tenant_id` on every row) — change it if you decide otherwise.

## 1. Design principles
1. **Every tenant-owned row has `tenant_id NOT NULL`**, and it is part of every index and FK.
2. **Isolation is enforced by the database, not by developer discipline:** Postgres Row-Level Security (RLS).
3. **Postgres is truth; Redis/Qdrant are rebuildable** (ADR-3). Anything in them must be derivable from Postgres + object storage.
4. **Append-only where it matters:** audit log and document versions are never updated in place.
5. IDs are UUIDv7 (time-sortable, safe to expose, no enumeration).

## 2. Entities (Postgres)

```mermaid
erDiagram
  TENANT ||--o{ USER : has
  TENANT ||--o{ COURSE : has
  COURSE ||--o{ ENROLLMENT : has
  USER ||--o{ ENROLLMENT : joins
  COURSE ||--o{ DOCUMENT : contains
  DOCUMENT ||--o{ DOCUMENT_VERSION : versions
  DOCUMENT_VERSION ||--o{ CHUNK : split_into
  USER ||--o{ CONVERSATION : starts
  COURSE ||--o{ CONVERSATION : scoped_to
  CONVERSATION ||--o{ MESSAGE : has
  MESSAGE ||--o| ESCALATION_TICKET : may_create
  TENANT ||--o{ USAGE_LEDGER : metered
  TENANT ||--o{ AUDIT_LOG : records
```

| Table | Key columns | Notes |
|---|---|---|
| `tenant` | id, name, status, plan, monthly_token_budget | Budget feeds gateway limits (LLD 03) |
| `app_user` | id, tenant_id, idp_subject, role (`student/instructor/admin/tutor`), status | `idp_subject` = OIDC `sub`; no passwords stored |
| `course` | id, tenant_id, title, term | Retrieval scope = course |
| `enrollment` | tenant_id, course_id, user_id, role_in_course | Decides who can ask about which course |
| `document` | id, tenant_id, course_id, title, current_version_id, status | Logical file |
| `document_version` | id, tenant_id, document_id, version_no, object_key, sha256, status (`uploaded/processing/ready/failed`) | Immutable; sha256 dedupes re-uploads |
| `chunk` | id, tenant_id, version_id, ordinal, text, token_count, embedding_model | Text kept here so vectors can be rebuilt |
| `conversation` | id, tenant_id, user_id, course_id, created_at | |
| `message` | id, tenant_id, conversation_id, role, content_redacted, route (`cache/rule/small/large/human`), model, cited_chunk_ids[], tokens_in/out, cost_usd, latency_ms, trace_id | The telemetry row per answer |
| `escalation_ticket` | id, tenant_id, message_id, reason, status, assigned_tutor_id, resolution | HITL |
| `usage_ledger` | tenant_id, user_id, day, tokens_in, tokens_out, cost_usd | Aggregated; source for budgets |
| `audit_log` | id, tenant_id, actor_id, action, target, at, ip | Append-only, no UPDATE/DELETE grants |
| `outbox_event` | id, tenant_id, type, payload, created_at, published_at | Transactional outbox → queue/CDC (LLD 07) |

## 3. Tenant isolation: three layers
1. **Postgres RLS:** `ALTER TABLE x ENABLE RLS; POLICY USING (tenant_id = current_setting('app.tenant_id')::uuid)`.
   App connects as a non-owner role; each request does `SET LOCAL app.tenant_id = <jwt tenant claim>` in its transaction.
2. **Vector store:** every point carries `tenant_id` + `course_id` payload; the retriever API *cannot be called without*
   a filter (wrapper rejects it). Qdrant payload index on both fields.
3. **Cache:** key namespace `t:{tenant_id}:c:{course_id}:...` — a lookup can never cross tenants by construction.

**Mandatory test:** two tenants with identical documents; assert a tenant-A user can never retrieve, cache-hit or read tenant-B data
via API, vector search or cache (we add this as an automated test in the first coding stage).

## 4. Non-Postgres data
| Store | Content | Key / structure | Rebuild from |
|---|---|---|---|
| Object storage | raw uploads | `{tenant}/{course}/{doc}/{version}/{filename}` | — (truth for files) |
| Qdrant | one collection `chunks_v{embedding_model_version}` | point id = chunk id; payload tenant/course/doc/version | `chunk` table (re-embed) |
| Redis | semantic cache, rate counters, session | see LLD 03/07 | not needed |

Versioned collection name lets us swap embedding models by building a new collection and flipping an alias (zero downtime).

## 5. Retention & privacy
- Messages store **redacted** content (PII scrubbed before persist); raw never logged.
- Conversation retention default 180 days per tenant policy; hard-delete job + delete vectors/cache on user/doc removal (GDPR-style).
- Audit log retained 1 year, partitioned monthly.

## 6. Scaling notes
- Partition `message`, `audit_log`, `usage_ledger` by month.
- Index on `(tenant_id, conversation_id, created_at)`; never an index without `tenant_id` leading.
- Read replicas for tutor dashboards/analytics; hot path reads only primary/cache.

## 7. Trade-offs to understand
| Choice | Gain | Cost / risk |
|---|---|---|
| Shared DB + RLS | cheap, simple ops, easy cross-tenant admin | noisy neighbor; a policy bug leaks data → tests + least-privileged role |
| Chunk text in Postgres | rebuildable vectors, easy debugging | storage duplication |
| Outbox pattern | no lost events (DB write + event atomic) | extra table + relay process |
| UUIDv7 | index-friendly, non-guessable | 16 bytes vs 8 |

## 8. Decisions for you
1. Course-scoped retrieval only, or also allow "ask across all my courses"? (Affects filters and cache keys.)
2. Keep raw chat content for quality evals (opt-in per tenant), or redacted only?
3. Do you accept RLS (DB-enforced) as the primary isolation, with the three layers above?
