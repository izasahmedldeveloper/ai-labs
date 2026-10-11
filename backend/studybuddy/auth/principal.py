"""Who is calling, once the token has been verified (LLD 02 s4)."""
from __future__ import annotations

from dataclasses import dataclass

from ..pipeline import Identity


class AuthError(Exception):
    """The caller is not authenticated. `reason` is for logs and tests; it is NEVER sent to the client."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AuthUnavailable(Exception):
    """We could not decide (key server or user store down). The API answers 503, not 401: the user did nothing wrong."""


@dataclass(frozen=True)
class Principal:
    tenant_id: str      # from the VERIFIED token only
    user_id: str        # app_user.id (what row-level security uses)
    subject: str        # IdP `sub`
    role: str           # app_user.role: the database is the authority, not the token
    jti: str | None = None

    def identity(self) -> Identity:
        return Identity(tenant_id=self.tenant_id, user_id=self.user_id, role=self.role)
