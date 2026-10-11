"""Metrics endpoints. `/metrics` is platform-wide and needs a scrape token; `/tenant/metrics` shows a tenant admin or
instructor only their own tenant's series (LLD 11 s10)."""
from __future__ import annotations

import hmac
import inspect

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .metrics import Registry

PROM_TYPE = "text/plain; version=0.0.4"


def add_metrics_routes(app: FastAPI, registry: Registry, *, authenticate, scrape_token: str | None) -> None:
    if scrape_token:
        @app.get("/metrics")
        async def metrics(request: Request):
            given = request.headers.get("authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {scrape_token}".encode()):
                raise HTTPException(status_code=404)  # do not reveal the endpoint
            return PlainTextResponse(registry.render(), media_type=PROM_TYPE)

    async def user(request: Request):
        r = authenticate(request)
        return await r if inspect.isawaitable(r) else r

    @app.get("/tenant/metrics")
    async def tenant_metrics(u=Depends(user)):
        if u.identity.role not in ("tenant_admin", "instructor"):
            raise HTTPException(status_code=404)
        return PlainTextResponse(registry.render(tenant_id=u.identity.tenant_id), media_type=PROM_TYPE)
