# Azure infrastructure for StudyBuddy

What to create by hand in your Azure subscription, what the code already supports, and what is still to build.
Names are examples. Costs are not estimated here: check the Azure pricing calculator for your region.

## Mapping

| Need | Azure resource | Code status |
|---|---|---|
| Relational data, row-level security | **Azure Database for PostgreSQL – Flexible Server** (v15+) | Ready. Run `bootstrap` then `migrate` once (see below) |
| Uploaded files | **Storage account** (Blob, one private container `documents`) | **Ready**: `AzureBlobObjectStore`, tested against the Azurite emulator |
| Login (JWT, RS256) | **Microsoft Entra ID** app registration | Verifier ready; needs two settings and one claim mapping (see Login) |
| Run the API and worker | **Azure Container Apps** + **Container Registry** | Ready: `python -m studybuddy.service` (needs a Dockerfile, not written yet) |
| Secrets | **Key Vault** | Settings are read from environment variables; Container Apps can fill them from Key Vault secrets |
| Logs and alerts | **Log Analytics workspace** (created with the Container Apps environment) | Logs are JSON lines on stdout and land there automatically. Alerts are only logged |
| Search index (vectors + keywords) | **Qdrant** in a Container App with an **Azure Files** volume (single replica) | `QdrantChunkStore` already implements search and the ingestion writer; it is **not wired into the service** yet (still in memory) |
| Event queue (outbox to workers, retries, dead letters) | **Storage Queues** in the same storage account (or Service Bus) | **Not built** (still in memory) |
| Cache and token denylist | **Azure Managed Redis** | **Not built**, and optional for one replica |
| Chat model, embeddings | **OpenAI API key** (you have this) | Chat ready. **Embeddings are a word-hashing placeholder**: an OpenAI embedder is still to build |

## Create now (first deployment)

1. **Resource group**, e.g. `rg-studybuddy`, in one region (put everything in the same region).
2. **PostgreSQL Flexible Server**: PostgreSQL 15 or 16, a small burstable size is fine to start. Allow access from the Container Apps environment (public access with a firewall rule, or a VNet). Create database `studybuddy`. Keep the admin login for setup only.
3. **Storage account** (Standard, LRS, StorageV2), `Allow Blob anonymous access` **off**. Create container `documents` (the service also creates it if missing). Later: a lifecycle rule is not needed, the cleanup job deletes blobs.
4. **Key Vault** for: the OpenAI key, the database passwords for the four service roles, the scrape token.
5. **Container Registry** (Basic) and a **Container Apps environment** (this also creates the Log Analytics workspace).
6. **Microsoft Entra app registration** for the API (see Login).
7. **Container App `studybuddy-api`**: 1 replica for now (see Limits), external ingress on port 8000, system-assigned **managed identity**, then grant that identity **Storage Blob Data Contributor** on the storage account and **Key Vault Secrets User** on the vault.

Do **not** create yet: Redis, Service Bus, Front Door, private endpoints. Add them when the matching code exists or traffic needs them.

## Database setup (one time)

The service connects as four roles (`sb_app`, `sb_auth`, `sb_worker`, `sb_relay`), never as the admin. Using the admin login:

```python
import psycopg
from studybuddy.db.migrate import bootstrap, migrate
conn = psycopg.connect("<admin connection string>", autocommit=True)
bootstrap(conn, passwords={"sb_app": "...", "sb_auth": "...", "sb_worker": "...", "sb_relay": "..."})
migrate(conn)
```

On Azure Flexible Server the admin is a member of `azure_pg_admin`, which can create roles, but this has not been run against a real Flexible Server: expect to adjust role ownership if `bootstrap` complains. Row-level security is enforced by these roles, so the admin must never be used by the app.

## Settings the service reads

| Setting | Value |
|---|---|
| `STUDYBUDDY_DSN_APP`, `_AUTH`, `_WORKER`, `_RELAY` | Connection strings, one per role, with `sslmode=require` |
| `STUDYBUDDY_BLOB_ACCOUNT_URL` | `https://<account>.blob.core.windows.net` (uses the managed identity) |
| `STUDYBUDDY_BLOB_CONTAINER` | `documents` |
| `STUDYBUDDY_JWT_ISSUER` | Entra v2 issuer: `https://login.microsoftonline.com/<directory-id>/v2.0` |
| `STUDYBUDDY_JWT_AUDIENCE` | The API's Application ID URI or client id |
| `STUDYBUDDY_JWKS_URL` | `https://login.microsoftonline.com/<directory-id>/discovery/v2.0/keys` |
| `STUDYBUDDY_JWT_MAX_LIFETIME_S` | `5400` (Entra access tokens live 60-90 minutes; the default of 900 would reject them) |
| `STUDYBUDDY_TENANTS` | Comma-separated StudyBuddy tenant ids, for the SLA and cleanup jobs |
| `STUDYBUDDY_SCRAPE_TOKEN` | Random string guarding `/metrics` |
| `OPENAI_API_KEY` | From Key Vault |

## Login with Entra: what is missing

- The service reads the tenant from a **`tenant_id` claim** and roles from a **`roles` claim**. Entra's own `tid` claim is the Azure directory, not a StudyBuddy tenant. Define **app roles** (`student`, `tutor`, `instructor`; never `tenant_admin`, which the database owns) so `roles` is filled, and add a `tenant_id` claim through an optional claim or a claims-mapping policy. For one school in one directory you can map a fixed value. Several schools in different directories need a small change in `auth/bearer.py` to derive the tenant from `tid`.
- JWKS keys are fetched from the URL above and cached. Entra tokens are RS256, which is allowed.

## Limits of this first deployment

- Run **one replica**. The search index, cache, broker and token denylist are in process memory: a restart loses the index (documents would have to be re-uploaded), and two replicas would each see only part of the data.
- The embedder is a placeholder, so answers will not be good until a real embedding model is wired in.
- No Dockerfile or deployment template yet.

## Build order I recommend

1. Dockerfile, plus a Bicep or `az` script for the resources above.
2. OpenAI embeddings (needed for real retrieval).
3. Qdrant in Container Apps: wire `QdrantChunkStore` into the service as retriever and ingestion index, so the index survives restarts.
4. Storage Queues broker, so queued work survives restarts and allows more than one replica.
5. Redis for the cache and denylist.
