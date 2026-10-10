# LLD 02 — Authentication, authorization & roles

Depends on LLD 01 (tenant, app_user, enrollment, escalation_ticket, RLS).

## 1. Concepts you should be able to explain after this doc
- **Authentication (AuthN):** who are you? Done by the Identity Provider (IdP), never by our app.
- **Authorization (AuthZ):** what may you do? Done by *our* app + database, using claims from the token.
- **OIDC/OAuth2:** the IdP issues a signed **JWT**; our services verify the signature offline with the IdP's public keys (JWKS), so no IdP call per request.
- **SSO:** each tenant (university) connects its own IdP (e.g. Azure AD, Google Workspace); Keycloak brokers them so we speak one protocol.

## 2. Flow (Authorization Code + PKCE for the SPA)
```mermaid
sequenceDiagram
  participant U as Browser (React)
  participant K as Keycloak (broker)
  participant T as Tenant IdP (optional SSO)
  participant G as API Gateway
  participant A as Orchestrator API
  participant D as Postgres (RLS)
  U->>K: /authorize (PKCE challenge, realm=tenant)
  K->>T: federate login (if tenant uses SSO)
  T-->>K: identity
  K-->>U: auth code
  U->>K: /token (code + PKCE verifier)
  K-->>U: access JWT (15 min) + refresh token (rotating)
  U->>G: API call, Bearer JWT
  G->>G: verify signature, exp, aud, issuer (JWKS cached)
  G->>A: forward + trusted headers (tenant, user, role)
  A->>A: authorize (role + course enrollment)
  A->>D: BEGIN; SET LOCAL app.tenant_id, app.user_id, app.role; query
```

## 3. Tenant model in Keycloak
**One realm per tenant** vs **one realm + tenant claim**:
| | Realm per tenant | Single realm, tenant attribute |
|---|---|---|
| Per-tenant SSO config / branding | easy | awkward |
| Ops at 1000s of tenants | heavy | light |
| Blast radius of misconfig | small | large |
Decision: **single realm, one Keycloak *organization*/identity-provider per tenant, `tenant_id` as a token claim** (scales, matches shared-DB model). Revisit for very large enterprise tenants.

## 4. Token contents (claims we rely on)
`iss, aud=studybuddy-api, sub (→ app_user.idp_subject), exp (≤15 min), tenant_id, roles[]`.
We **never trust a tenant_id sent by the client in body/query**—only the one from the verified token.
First login: if no `app_user` row for (`tenant_id`,`sub`), create it (JIT provisioning) with role from IdP group mapping.

## 5. Roles & permissions (RBAC + enrollment)
| Role | Can |
|---|---|
| student | ask in *enrolled* courses; see own conversations |
| instructor | upload/replace/delete documents in *own* courses; view course analytics (no raw chat) |
| tutor | open **only assigned** tickets and the conversation attached (decision D4) |
| tenant_admin | manage users, courses, enrollments, budget, SSO settings; view audit log |
| platform_operator (us) | no access to tenant content; operational metrics only; break-glass access is time-boxed + audited |

Permission check = `role allows action` **AND** `resource in scope` (course enrollment/ownership/ticket assignment).
Role alone is never enough (prevents instructor A editing instructor B's course).

Implementation shape: a single `authorize(principal, action, resource)` function in one module; endpoints declare the
action they need via a dependency. Policy table lives in code at first; move to a policy engine (OPA/Cedar) only if rules multiply.

## 6. Defense in depth (three gates)
1. **Gateway:** token valid? coarse route rules (e.g. `/admin/*` needs tenant_admin).
2. **App:** fine-grained `authorize()` with enrollment/ownership checks.
3. **DB:** RLS using `app.tenant_id`, plus ticket-assignment policy on messages (D4).
A bug in one gate must not leak data because the next still holds.

## 7. Service-to-service & workers
- Internal calls and queue messages carry `tenant_id` explicitly; workers set `SET LOCAL app.tenant_id` before touching Postgres (same RLS applies).
- Workers authenticate with their own service identity (client-credentials), not a user token.
- No shared "superuser" DB role in application code. Migrations run as a separate owner role.

## 8. Session & token lifecycle
- Access token 15 min; refresh token rotating with reuse detection (stolen refresh token → revoke family).
- Logout / user disable: access tokens remain valid until expiry, so for instant revoke the gateway checks a small Redis denylist keyed by `sub`/`jti` (only for disabled users).
- SPA keeps tokens in memory (not localStorage) to limit XSS theft; silent refresh via refresh token flow.

## 9. Audit
Log (append-only `audit_log`): login, role change, document upload/delete, enrollment change, ticket open, **every raw-chat read**, break-glass use.

## 10. Failure modes & tests
- IdP/JWKS down → gateway uses cached keys; new logins fail but existing sessions keep working (degraded, alerted).
- Clock skew → allow 60 s leeway.
- Tests: expired/forged/wrong-audience token rejected; tenant claim tampering impossible; student cannot ask in unenrolled course; instructor cannot touch another's course; tutor cannot read an unassigned conversation; RLS blocks cross-tenant even if app `authorize()` is bypassed.

## 11. Trade-offs
| Choice | Gain | Cost |
|---|---|---|
| Offline JWT verification | no IdP in hot path, low latency | revocation delay → short TTL + denylist |
| Keycloak (self-run) | free, SSO brokering, OIDC standard | we operate it; managed IdP (Auth0/Cognito) is the alternative |
| Single realm + claim | scales | one misconfig affects all tenants |
| In-code policy first | simple, testable | may outgrow → OPA later |

## 12. Decisions for you
1. Keycloak self-hosted (learn the standard) vs a managed IdP (less ops)? Default: Keycloak.
2. Do we need per-tenant university SSO in v1, or just email/password + Google login first? Default: Google/email first, SSO-ready design.
3. Is 15-minute access token + denylist acceptable, or do you want instant revocation everywhere (costlier)?
