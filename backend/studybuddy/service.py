"""The real service: settings from the environment, every part built and connected, one object to run or stop.

    STUDYBUDDY_DSN_APP=... STUDYBUDDY_DSN_AUTH=... STUDYBUDDY_DSN_WORKER=... STUDYBUDDY_DSN_RELAY=... \\
    STUDYBUDDY_JWT_ISSUER=... STUDYBUDDY_JWT_AUDIENCE=... STUDYBUDDY_JWKS_URL=... STUDYBUDDY_TENANTS=<uuid>,<uuid> \\
    OPENAI_API_KEY=... python -m studybuddy.service

Each database connection uses its own least-privilege role (LLD 01). `api/dev.py` stays the no-database demo.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Mapping

from .api.app import create_app
from .api.routes import add_routes
from .auth import BearerAuth, HttpJwks, InMemoryDenylist, JwtVerifier, PgUserDirectory
from .cache import InMemoryCacheStore, SemanticCache
from .db import ConversationRepo, Database, EnrollmentRepo
from .escalation import FaqService, Notifier, PgEscalation, SlaMonitor, TicketService
from .guardrails import Policy, RuleInputGuardrails, RuleOutputGuardrails
from .guardrails.policy import ORDINAL_CITATION
from .ingestion import (AzureBlobObjectStore, CleanupJob, DocumentService, IngestionConsumer, IngestionWorker, InMemoryBroker, InMemoryObjectStore, MemoryIndex,
                        OutboxRelay, PgVersionRepo)
from .observability import (AlertEngine, EventSink, JsonLogger, MemoryExporter, Registry, RequestObserver, Telemetry, add_metrics_routes,
                            chain)
from .pipeline import Orchestrator, PipelineConfig, fakes
from .retrieval import HybridRetriever
from .retrieval.fakes import HashingEmbedder, OverlapReranker
from .router import Router
from .workers import WorkerRunner

ROLES = ("app", "auth", "worker", "relay")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    dsn: Mapping[str, str]
    jwt_issuer: str
    jwt_audience: str
    jwks_url: str | None
    tenants: tuple[str, ...] = ()
    scrape_token: str | None = None
    openai_key: str | None = None
    anthropic_key: str | None = None
    fake_model: bool = False
    pool_size: int = 8
    alert_every_s: float = 30.0
    worker_interval_s: float = 1.0
    max_traces: int = 500
    blob_connection_string: str | None = None   # Azurite or a storage account key
    blob_account_url: str | None = None         # https://<account>.blob.core.windows.net (managed identity)
    blob_container: str = "documents"
    tenant_map: Mapping[str, str] = field(default_factory=dict)   # Entra directory id -> StudyBuddy tenant id
    jwt_max_lifetime_s: int = 900               # Microsoft Entra access tokens live 60-90 minutes: set 5400

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        missing = [k for k in [f"STUDYBUDDY_DSN_{r.upper()}" for r in ROLES] + ["STUDYBUDDY_JWT_ISSUER", "STUDYBUDDY_JWT_AUDIENCE"] if not env.get(k)]
        if missing:
            raise ConfigError("missing settings: " + ", ".join(missing))
        return cls(
            dsn={r: env[f"STUDYBUDDY_DSN_{r.upper()}"] for r in ROLES},
            jwt_issuer=env["STUDYBUDDY_JWT_ISSUER"], jwt_audience=env["STUDYBUDDY_JWT_AUDIENCE"],
            jwks_url=env.get("STUDYBUDDY_JWKS_URL") or None,
            tenants=tuple(t.strip() for t in env.get("STUDYBUDDY_TENANTS", "").split(",") if t.strip()),
            scrape_token=env.get("STUDYBUDDY_SCRAPE_TOKEN") or None,
            openai_key=env.get("OPENAI_API_KEY") or None, anthropic_key=env.get("ANTHROPIC_API_KEY") or None,
            fake_model=env.get("STUDYBUDDY_FAKE_MODEL") == "1",
            blob_connection_string=env.get("STUDYBUDDY_BLOB_CONNECTION_STRING") or None,
            blob_account_url=env.get("STUDYBUDDY_BLOB_ACCOUNT_URL") or None,
            blob_container=env.get("STUDYBUDDY_BLOB_CONTAINER") or "documents",
            tenant_map=_parse_map(env.get("STUDYBUDDY_TENANT_MAP", "")),
            jwt_max_lifetime_s=int(env.get("STUDYBUDDY_JWT_MAX_LIFETIME_S") or 900))


def _parse_map(text: str) -> dict[str, str]:
    out = {}
    for part in (p.strip() for p in text.split(",") if p.strip()):
        k, sep, v = part.partition("=")
        if not sep or not k.strip() or not v.strip():
            raise ConfigError("STUDYBUDDY_TENANT_MAP must look like <directory-id>=<tenant-uuid>,...")
        out[k.strip()] = v.strip()
    return out


class LogChannel:
    """Notification channel that only logs that a notification went out (no text, LLD 10). Replace with email/chat."""
    name = "log"

    def __init__(self, logger: JsonLogger):
        self.logger = logger

    async def send(self, tenant_id, user_id, notification):
        self.logger.log("notification.sent", tenant=tenant_id, user=user_id)


@dataclass
class Service:
    app: object
    runner: WorkerRunner
    alerts: AlertEngine
    registry: Registry
    exporter: MemoryExporter
    dbs: dict
    settings: Settings
    errors: list = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list = field(default_factory=list)
    closers: list = field(default_factory=list)

    def start(self) -> None:
        """Start the worker loop and the alert loop in the background."""
        self._tasks = [asyncio.create_task(self.runner.run_forever(self._stop, self.settings.worker_interval_s)),
                       asyncio.create_task(self._alert_loop())]

    async def _alert_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.alerts.evaluate()
            except Exception as exc:
                self.errors.append(("alerts", exc))
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.alert_every_s)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for db in self.dbs.values():
            await db.close()
        for c in self.closers:
            await c.close()


def _generator(settings: Settings):
    if settings.openai_key or settings.anthropic_key:
        from .api.dev import build_dev_gateway
        from .modelgateway import GatewayGenerator, PromptBuilder
        output = RuleOutputGuardrails(Policy(citation_pattern=ORDINAL_CITATION, citation_ids="ordinal"))
        return GatewayGenerator(build_dev_gateway(settings.openai_key, settings.anthropic_key), PromptBuilder()), PipelineConfig(model_ttft_timeout_s=8.0), output
    if settings.fake_model:
        return fakes.FakeGenerator(token_delay=0.05), PipelineConfig(), RuleOutputGuardrails()
    raise ConfigError("no model configured: set OPENAI_API_KEY or ANTHROPIC_API_KEY (or STUDYBUDDY_FAKE_MODEL=1 for a demo)")


async def create_service(settings: Settings, *, jwks=None, http_client=None, clock=None) -> Service:
    """Connect the databases and assemble everything. `jwks` overrides key fetching (tests)."""
    generator, pipeline_cfg, output = _generator(settings)  # fail before opening any connection
    if jwks is None:
        if not settings.jwks_url:
            raise ConfigError("STUDYBUDDY_JWKS_URL is required")
        import httpx
        jwks = HttpJwks(settings.jwks_url, http_client or httpx.AsyncClient())
    dbs: dict[str, Database] = {}
    try:
        for role in ROLES:
            dbs[role] = await Database.connect(settings.dsn[role], max_size=settings.pool_size)
    except Exception:
        for d in dbs.values():
            await d.close()
        raise

    registry = Registry(**({"clock": clock} if clock else {}))
    telemetry = Telemetry(registry)
    logger = JsonLogger()
    sink = EventSink(registry, logger=logger, telemetry=telemetry)
    exporter = MemoryExporter(settings.max_traces)
    observer = RequestObserver(registry, logger=logger, exporter=exporter, telemetry=telemetry)

    embedder = HashingEmbedder()   # PLACEHOLDER embedder/reranker until a real embedding service exists
    closers: list = []
    if settings.blob_connection_string or settings.blob_account_url:
        objects = (AzureBlobObjectStore.from_connection_string(settings.blob_connection_string, settings.blob_container)
                   if settings.blob_connection_string else
                   AzureBlobObjectStore.from_account_url(settings.blob_account_url, settings.blob_container))
        closers.append(objects)
        try:
            await objects.ensure_container()
        except Exception:
            for d in dbs.values():
                await d.close()
            await objects.close()
            raise
    else:
        objects = InMemoryObjectStore()
    index, broker = MemoryIndex(), InMemoryBroker()
    guardrails = RuleInputGuardrails(on_event=sink)
    output.on_event = sink
    retriever = HybridRetriever(index.store, embedder, reranker=OverlapReranker({}), on_event=sink)
    versions = _StoreVersions(index.store)
    cache = SemanticCache(InMemoryCacheStore(), embedder, versions, policy_version=lambda: guardrails.policy_version)
    escalation = PgEscalation(dbs["app"], on_event=sink)
    conversations = ConversationRepo(dbs["app"])
    orchestrator = Orchestrator(
        guardrails=guardrails, cache=cache, router=Router(embedder=embedder), retriever=retriever, generator=generator,
        output_guardrails=output, escalation=escalation, config=pipeline_cfg,
        on_complete=chain(observer.on_complete, conversations.record_turn))

    verifier = JwtVerifier(issuer=settings.jwt_issuer, audience=settings.jwt_audience, jwks=jwks,
                         max_lifetime_s=settings.jwt_max_lifetime_s)
    auth = BearerAuth(verifier, PgUserDirectory(dbs["auth"]), InMemoryDenylist(),
                      tenant_map=dict(settings.tenant_map) or None,
                      on_reject=lambda reason: logger.log("auth.rejected", reason=reason, level="warning"))
    enrollment = EnrollmentRepo(dbs["app"])
    app = create_app(orchestrator, authenticate=auth, check_enrollment=enrollment.is_enrolled, load_conversation=conversations.load)

    san = guardrails.sanitize
    add_routes(app, auth, documents=DocumentService(dbs["app"], objects), tickets=TicketService(dbs["app"], escalation, san),
               faq=FaqService(dbs["app"], cache, san), conversations=conversations)
    add_metrics_routes(app, registry, authenticate=auth, scrape_token=settings.scrape_token)

    @app.get("/readyz")
    async def readyz():
        from fastapi import HTTPException
        try:
            async with dbs["app"].pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            raise HTTPException(status_code=503, detail="not ready") from None
        return {"status": "ready"}

    errors: list = []

    def on_error(where, exc):
        errors.append((where, exc))
        logger.log("worker.error", level="error", stage=where, error_type=type(exc).__name__)

    worker = IngestionWorker(PgVersionRepo(dbs["worker"]), objects, index, embedder, broker, on_event=sink, on_dead_letter=sink.dead_letter)
    tenants = list(settings.tenants)

    async def tenant_ids():
        return tenants
    runner = WorkerRunner(relay=OutboxRelay(dbs["relay"], broker), broker=broker, ingestion_consumer=IngestionConsumer(broker, worker),
                          notifier=Notifier(dbs["worker"], [LogChannel(logger)]), sla=SlaMonitor(dbs["worker"]),
                          cleanup=CleanupJob(dbs["worker"], index, objects), tenants=tenant_ids, on_error=on_error)
    alerts = AlertEngine(registry, telemetry, notify=lambda a, state: logger.log(
        "alert", level="error" if state == "firing" else "info", alert=a.name, severity=a.severity, status=state, value=a.value))
    return Service(app, runner, alerts, registry, exporter, dbs, settings, errors, closers=closers)


class _StoreVersions:
    def __init__(self, store):
        self._store = store

    async def current_version(self, tenant_id, course_id, document_id):
        return self._store.current_version(tenant_id, course_id, document_id)


async def serve(settings: Settings, host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    svc = await create_service(settings)
    svc.start()
    try:
        await uvicorn.Server(uvicorn.Config(svc.app, host=host, port=port, log_level="info")).serve()
    finally:
        await svc.stop()


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}")
    asyncio.run(serve(settings, port=int(os.environ.get("PORT", "8000"))))


if __name__ == "__main__":
    main()
