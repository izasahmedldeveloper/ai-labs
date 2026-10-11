# Azure infrastructure for StudyBuddy

What to create by hand in your Azure subscription, what the code already supports, and what is still to build.
Names are examples. Costs are not estimated here: check the Azure pricing calculator for your region.

## Mapping

| Need | Azure resource | Code status |
|---|---|---|
| Relational data, row-level security | **Azure Database for PostgreSQL – Flexible Server** (v15+) | Ready. Run `bootstrap` then `migrate` once (see below) |
| Uploaded files | **Storage account** (Blob, one private container `documents`) | **Ready**: `AzureBlobObjectStore`, tested against the Azurite emulator |
| Login (JWT, RS256) | **Keycloak** in a Container App for now (Entra needs an app registration you cannot create) | Service side ready for either; Keycloak setup is configuration only (see Login) |
| Run the API and worker | **Azure Container Apps** + **Container Registry** | Ready: `python -m studybuddy.service` (needs a Dockerfile, not written yet) |
| Secrets | **Container App secrets** (Key Vault needs a role assignment you lack) | Settings are read from environment variables |
| Logs and alerts | **Log Analytics workspace** (created with the Container Apps environment) | Logs are JSON lines on stdout and land there automatically. Alerts are only logged |
| Search index (vectors + keywords) | **Qdrant** in a Container App with an **Azure Files** volume (single replica) | `QdrantChunkStore` already implements search and the ingestion writer; it is **not wired into the service** yet (still in memory) |
| Event queue (outbox to workers, retries, dead letters) | **Storage Queues** in the same storage account (or Service Bus) | **Not built** (still in memory) |
| Cache and token denylist | **Azure Managed Redis** | **Not built**, and optional for one replica |
| Chat model, embeddings | **OpenAI API key** (you have this) | Chat ready. **Embeddings are a word-hashing placeholder**: an OpenAI embedder is still to build |

## Create now (first deployment), with Contributor access only

Contributor can create resources but **cannot create role assignments** and **cannot create Entra app registrations or service principals**. So this plan avoids both: no managed-identity role grants (secrets and keys are used instead) and no Entra login (we run our own identity provider, below).

1. **Resource group**, e.g. `rg-studybuddy`, in one region (put everything in the same region).
2. **PostgreSQL Flexible Server**: PostgreSQL 15 or 16, a small burstable size is fine to start. Allow access from the Container Apps environment (public access with a firewall rule, or a VNet). Create two databases: `studybuddy` and `keycloak`. Keep the admin login for setup only.
3. **Storage account** (Standard, LRS, StorageV2), `Allow Blob anonymous access` **off**. The service creates the `documents` container if missing. Auth is by connection string (`STUDYBUDDY_BLOB_CONNECTION_STRING`), because granting a managed identity the Blob role needs a role assignment you do not have. Keep the string as a Container App secret.
4. **Container Registry** (Basic) with the **admin user enabled**. Container Apps then pulls with that username and password, because pulling with a managed identity needs the AcrPull role assignment.
5. **Container Apps environment** (this also creates the Log Analytics workspace that collects the JSON logs).
6. **Container App `studybuddy-api`**: 1 replica for now (see Limits), external ingress on port 8000. All settings, including the OpenAI key and database passwords, go in as **Container App secrets** (no Key Vault needed; Key Vault with role-based access also needs a role assignment).
7. **Container App `keycloak`**: the identity provider (see Login). Backed by the `keycloak` database.

Do **not** create yet: Redis, Service Bus, Front Door, private endpoints. Add them when the matching code exists or traffic needs them. If you later get an administrator to add role assignments, switch storage to the managed identity (`STUDYBUDDY_BLOB_ACCOUNT_URL`) and the secrets to Key Vault.

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
| `STUDYBUDDY_BLOB_CONNECTION_STRING` | Storage account connection string (secret). With a managed identity and an admin's role grant, use `STUDYBUDDY_BLOB_ACCOUNT_URL` instead |
| `STUDYBUDDY_BLOB_CONTAINER` | `documents` |
| `STUDYBUDDY_JWT_ISSUER`, `_AUDIENCE`, `STUDYBUDDY_JWKS_URL` | From Keycloak (see Login) |
| `STUDYBUDDY_TENANTS` | Comma-separated StudyBuddy tenant ids, for the SLA and cleanup jobs |
| `STUDYBUDDY_SCRAPE_TOKEN` | Random string guarding `/metrics` |
| `OPENAI_API_KEY` | Container App secret |

## Login without an Entra app registration

You cannot create an app registration or service principal, and Entra sign-in (including Container Apps "Authentication") needs one. The service only needs three things from any identity provider: an **issuer**, an **audience** and a **JWKS URL** that serves RS256 signing keys. So until an administrator grants you one app registration, run **Keycloak** as the identity provider, in a Container App you can create yourself:

1. Image `quay.io/keycloak/keycloak`, command `start`, database `keycloak` on your PostgreSQL server (`KC_DB=postgres` and its URL, user and password as secrets), behind the Container Apps HTTPS ingress with `KC_PROXY_HEADERS=xforwarded` and `KC_HOSTNAME` set to its public URL. Set the bootstrap admin user and password as secrets and change them on first login. Keycloak versions differ on these variable names: check the Keycloak "containers" and "reverse proxy" guides for your version.
2. Create a realm `studybuddy` and a client for the web app (public client, authorization code with PKCE; use the standard flow). Add users, or turn on self-registration.
3. In the client's dedicated scope add two **mappers**: an *Audience* mapper adding `studybuddy-api` to the access token, and a *Hardcoded claim* mapper `tenant_id` with your StudyBuddy tenant uuid (one school = one realm = one fixed value; several schools would use one realm each or a user attribute). Leave `STUDYBUDDY_TENANT_MAP` unset; the `tenant_id` claim is used.
4. Service settings:

| Setting | Value |
|---|---|
| `STUDYBUDDY_JWT_ISSUER` | `https://<keycloak-host>/realms/studybuddy` |
| `STUDYBUDDY_JWT_AUDIENCE` | `studybuddy-api` |
| `STUDYBUDDY_JWKS_URL` | `https://<keycloak-host>/realms/studybuddy/protocol/openid-connect/certs` |
| `STUDYBUDDY_JWT_MAX_LIFETIME_S` | leave the default 900 (Keycloak access tokens last 5 minutes) |

Everyone starts as a student; instructors and admins are promoted in the database, which owns roles. Keycloak can later be switched to sign people in with your company's Entra account (identity brokering), which still needs an app registration, so that waits for the administrator.

**When an administrator can give you one app registration**, change only the issuer, audience and JWKS settings to the Entra values (`https://login.microsoftonline.com/<directory-id>/v2.0`, the Application ID URI, `.../discovery/v2.0/keys`), set `STUDYBUDDY_JWT_MAX_LIFETIME_S=5400` and `STUDYBUDDY_TENANT_MAP=<directory-id>=<tenant uuid>`. No code change.

Not yet tested: the service has only been tested with self-made tokens, and Keycloak has not been run at all. Real tokens may need a small adjustment on first use.

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
