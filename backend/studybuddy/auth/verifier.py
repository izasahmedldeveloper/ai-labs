"""JWT verification (LLD 02 s2, s4, s10).

Only asymmetric algorithms are accepted; `alg` is checked against our allow-list BEFORE the key is used, so the
classic downgrade tricks ("none", or HS256 signed with the public key) never reach the crypto. Every failure is an
`AuthError` with an internal reason; the client only ever sees a generic 401.
"""
from __future__ import annotations

import jwt

from .principal import AuthError

ALLOWED_ALGORITHMS = ("RS256", "ES256")


class JwtVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks, leeway_s: int = 60, max_lifetime_s: int = 900,
                 algorithms: tuple[str, ...] = ALLOWED_ALGORITHMS):
        if not set(algorithms) <= set(ALLOWED_ALGORITHMS):
            raise ValueError("only asymmetric algorithms (RS256, ES256) may be enabled")
        self.issuer, self.audience, self.jwks = issuer, audience, jwks
        self.leeway_s, self.max_lifetime_s, self.algorithms = leeway_s, max_lifetime_s, algorithms

    async def verify(self, token: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("malformed token") from None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in self.algorithms:
            raise AuthError("algorithm not allowed")
        if not isinstance(kid, str) or not kid:
            raise AuthError("missing kid")
        jwk = await self.jwks.get_key(kid)  # may raise AuthUnavailable
        if jwk is None:
            raise AuthError("unknown signing key")
        try:
            key = jwt.PyJWK(jwk).key
            claims = jwt.decode(token, key, algorithms=[alg], audience=self.audience, issuer=self.issuer,
                                leeway=self.leeway_s, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {type(exc).__name__}") from None
        if claims["exp"] - claims["iat"] > self.max_lifetime_s + self.leeway_s:
            raise AuthError("token lifetime too long")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise AuthError("bad subject")
        return claims
