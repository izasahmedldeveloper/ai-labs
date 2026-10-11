# Azure infrastructure for StudyBuddy

What to create by hand in your Azure subscription, what the code already supports, and what is still to build.
Names are examples. Costs are not estimated here: check the Azure pricing calculator for your region.

## Mapping

| Need | Azure resource | Code status |
|---|---|---|
| Relational data, row-level security | **Azure Database for PostgreSQL – Flexible Server** (v15+) | Ready. Run `bootstrap` then `migrate` once (see below) |
| Uploaded files | **Storage account** (Blob, one private container `documents`) | **Ready**: `AzureBlobObjectStore`, tested against the Azurite emulator |
| Login (JWT, RS256) | **Microsoft Entra ID**: one app registration in your existing directory | Ready, including the directory-to-tenant map (see Login) |
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
6. **One Microsoft Entra app registration** for the API, in your existing directory (see Login; no new tenant needed).
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
| `STUDYBUDDY_TENANT_MAP` | `<directory-id>=<StudyBuddy tenant uuid>`; the tenant uuid is the `tenant.id` row you create in the database |
| `STUDYBUDDY_TENANTS` | Comma-separated StudyBuddy tenant ids, for the SLA and cleanup jobs |
| `STUDYBUDDY_SCRAPE_TOKEN` | Random string guarding `/metrics` |
| `OPENAI_API_KEY` | From Key Vault |

## Login with Entra when you cannot create a directory (tenant)

You do **not** need to create an Entra tenant. StudyBuddy uses your existing directory; the Azure subscription already belongs to one. What is needed from it:

1. **One app registration** (the API). If you cannot register apps yourself, ask an admin for the *Application Developer* role, or ask them to create it. Nothing else in the directory changes. In it: *Expose an API* (an Application ID URI such as `api://<client-id>`), and set `requestedAccessTokenVersion` to 2 in the manifest so tokens are v2 with the issuer below.
2. **Who may sign in**: that is the existing users of the directory. If the admin wants to limit it, they turn on *Assignment required* on the enterprise application and assign people or a group.
3. **No claims-mapping policy and no app roles are needed.** The tenant is taken from the directory id (`tid`) in the token through a server-side map, `STUDYBUDDY_TENANT_MAP=<directory-id>=<StudyBuddy tenant uuid>`; a `tenant_id` claim in the token is ignored when the map is set. Everyone signs in as a student; instructors and admins are promoted in the database (the database owns roles).
4. A client (the web app, or Postman for testing) needs a way to get a token for that API. A second app registration for the client is the usual way; if you cannot make one either, an admin can add the Azure CLI/Postman as a pre-authorized client, or you test with `az account get-access-token --resource api://<client-id>`.

If your admin refuses even one app registration, the fallback is to run a small identity provider of our own (for example Keycloak in a Container App) and point the same three settings at it. That is more to operate; ask the admin first.

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
