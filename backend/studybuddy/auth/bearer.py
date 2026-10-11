"""`Authorization: Bearer <jwt>` authenticator for `create_app(authenticate=...)` (LLD 02 s2, s4, s6 gate 1).

Order: header -> signature and claims -> tenant claim shape -> denylist -> user lookup (JIT) -> disabled check.
The tenant comes ONLY from the verified token. Failures are one generic 401 (reasons go to `on_reject` for logs);
infrastructure failures are 503.
"""
from __future__ import annotations

import re
import uuid
from typing import Callable

from fastapi import HTTPException, Request

from ..api.auth import Authenticated
from ..pipeline import BudgetState
from .denylist import Denylist
from .principal import AuthError, AuthUnavailable, Principal

_BEARER = re.compile(r"^Bearer ([A-Za-z0-9\-._~+/]+=*)$")


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Authentication required.", headers={"WWW-Authenticate": "Bearer"})


class BearerAuth:
    def __init__(self, verifier, directory, denylist: Denylist | None = None, *, on_reject: Callable[[str], None] | None = None,
                 budget_for_tenant: Callable[[str], BudgetState] | None = None):
        self.verifier, self.directory, self.denylist = verifier, directory, denylist
        self.on_reject, self.budget_for_tenant = on_reject or (lambda reason: None), budget_for_tenant

    async def principal(self, request: Request) -> Principal:
        m = _BEARER.match(request.headers.get("authorization", ""))
        if not m:
            raise AuthError("missing or malformed bearer header")
        claims = await self.verifier.verify(m.group(1))
        tenant = claims.get("tenant_id")
        try:
            tenant = str(uuid.UUID(tenant)) if isinstance(tenant, str) else None
        except ValueError:
            tenant = None
        if tenant is None:
            raise AuthError("missing or invalid tenant_id claim")
        roles = claims.get("roles", [])
        if not isinstance(roles, list):
            raise AuthError("invalid roles claim")
        subject, jti = claims["sub"], claims.get("jti")
        if self.denylist is not None:
            try:
                denied = await self.denylist.is_denied(tenant, subject, jti)
            except Exception:
                raise AuthUnavailable("denylist unavailable") from None  # revocation cannot be checked: fail closed
            if denied:
                raise AuthError("revoked")
        user = await self.directory.resolve(tenant, subject, roles)
        if user.status != "active":
            raise AuthError("user disabled")
        return Principal(tenant_id=tenant, user_id=user.user_id, subject=subject, role=user.role, jti=jti)

    async def __call__(self, request: Request) -> Authenticated:
        try:
            p = await self.principal(request)
        except AuthError as exc:
            self.on_reject(exc.reason)
            raise _unauthorized() from None
        except AuthUnavailable as exc:
            self.on_reject(f"unavailable: {exc}")
            raise HTTPException(status_code=503, detail="Service temporarily unavailable. Please try again.") from None
        budget = self.budget_for_tenant(p.tenant_id) if self.budget_for_tenant else BudgetState.OK
        return Authenticated(p.identity(), budget)
