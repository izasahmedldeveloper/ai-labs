"""Identity for the HTTP layer (LLD 02, LLD 03 section 2).

The orchestrator trusts `Identity` completely, so it must come from somewhere that already verified the
login token. In production the API gateway verifies the JWT and forwards the result as headers; this module
reads them. When LLD 02 is built, replace `GatewayHeaderAuth` with a verifier that checks the JWT itself:
`create_app` only needs a callable `(Request) -> Authenticated`.

Never read identity from the request body or from query parameters: the client is untrusted code.
"""
from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..pipeline import BudgetState, Identity

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")

HEADER_TENANT = "x-tenant-id"
HEADER_USER = "x-user-id"
HEADER_ROLE = "x-user-role"
HEADER_BUDGET = "x-budget-state"
HEADER_SECRET = "x-gateway-secret"


@dataclass(frozen=True)
class Authenticated:
    identity: Identity
    budget_state: BudgetState = BudgetState.OK


def unauthorized() -> HTTPException:
    # One generic message for every failure: do not tell an attacker which check failed.
    return HTTPException(status_code=401, detail="Authentication required.")


class GatewayHeaderAuth:
    """Reads identity from headers set by the trusted gateway.

    If `gateway_secret` is set, requests that do not carry it are rejected, so a caller that reaches the
    service directly (bypassing the gateway) cannot claim any tenant or user it likes.
    """

    def __init__(self, gateway_secret: str | None = None):
        self._secret = gateway_secret

    def __call__(self, request: Request) -> Authenticated:
        if self._secret is not None:
            supplied = request.headers.get(HEADER_SECRET, "")
            if not hmac.compare_digest(supplied.encode(), self._secret.encode()):
                raise unauthorized()
        tenant = request.headers.get(HEADER_TENANT, "")
        user = request.headers.get(HEADER_USER, "")
        role = request.headers.get(HEADER_ROLE, "")
        if not (_SAFE_ID.match(tenant) and _SAFE_ID.match(user) and _SAFE_ID.match(role)):
            raise unauthorized()
        try:
            budget = BudgetState(request.headers.get(HEADER_BUDGET, BudgetState.OK.value))
        except ValueError:
            raise unauthorized() from None
        return Authenticated(Identity(tenant_id=tenant, user_id=user, role=role), budget)
