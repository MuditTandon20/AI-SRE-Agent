import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from psycopg_pool import PoolClosed, PoolTimeout

from ai_sre_agent.config import Settings
from ai_sre_agent.database import create_pool
from ai_sre_agent.incidents import (
    Incident,
    IncidentCreate,
    IncidentCreated,
    IncidentPage,
    IncidentRepository,
)
from ai_sre_agent.migrate import SchemaError, check_schema
from ai_sre_agent.remediation import router as remediation_router
from ai_sre_agent.routes import router
from ai_sre_agent.safety import authorize
from ai_sre_agent.telemetry import WorkflowCollector

logger = logging.getLogger("ai_sre_agent")


def get_repository(request: Request) -> IncidentRepository:
    return IncidentRepository(request.app.state.pool)


Repository = Annotated[IncidentRepository, Depends(get_repository)]


def create_app(settings: Settings | None = None, *, pool_factory=create_pool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configuration = settings or Settings.from_env()
        app.state.settings = configuration
        pool = pool_factory(configuration)
        app.state.pool = pool
        try:
            await run_in_threadpool(
                pool.open,
                wait=True,
                timeout=configuration.database_startup_timeout_seconds,
            )
            yield
        finally:
            await run_in_threadpool(pool.close)

    app = FastAPI(title="AI SRE Agent", version="0.1.0-dev", lifespan=lifespan)
    app.include_router(router)
    app.include_router(remediation_router)
    public_paths = {
        "/",
        "/health",
        "/ready",
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
                "description": "Use the token for the route's operator, webhook, approver or delivery role.",
            }
            for path, operations in schema["paths"].items():
                if path not in public_paths:
                    for operation in operations.values():
                        if isinstance(operation, dict):
                            operation["security"] = [{"BearerAuth": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi

    @app.middleware("http")
    async def access_and_size(request: Request, call_next):
        is_approval = request.url.path.startswith("/remediation/") and request.url.path.endswith(
            ("/approve", "/revoke")
        )
        if (
            request.url.path not in public_paths
            and not request.url.path.startswith("/webhooks/")
            and not is_approval
        ):
            try:
                authorize(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        if request.method in {"POST", "PATCH", "PUT"}:
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 262144:
                    return JSONResponse(
                        status_code=413, content={"detail": "Request body too large"}
                    )
            request._body = bytes(chunks)
        return await call_next(request)

    registry = CollectorRegistry()
    registry.register(WorkflowCollector(app))
    GCCollector(registry=registry)
    PlatformCollector(registry=registry)
    ProcessCollector(registry=registry)
    requests = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["method", "route", "status"],
        registry=registry,
    )
    latency = Histogram(
        "http_request_duration_seconds",
        "Request latency in seconds",
        ["method", "route"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        registry=registry,
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            seconds = time.perf_counter() - start
            route = request.scope.get("route")
            route_path = route.path if route else "unmatched"
            requests.labels(request.method, route_path, str(status)).inc()
            latency.labels(request.method, route_path).observe(seconds)
            logger.info(
                "method=%s route=%s status=%s latency_ms=%.0f",
                request.method,
                route_path,
                status,
                seconds * 1000,
            )

    async def database_error(request: Request, exc: Exception):
        logger.warning("database_request_failed error_type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "Database unavailable or schema not ready"},
        )

    for error_type in (psycopg.Error, PoolTimeout, PoolClosed, SchemaError):
        app.add_exception_handler(error_type, database_error)

    @app.get("/")
    def root():
        return {"message": "AI SRE Agent API is running"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request):
        with request.app.state.pool.connection() as conn:
            check_schema(conn)
        return {"status": "ready"}

    @app.get("/crash")
    def crash():
        raise RuntimeError("Something went wrong")

    @app.get("/slow")
    def slow():
        time.sleep(5)
        return {"status": "finally done"}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/incidents", status_code=201, response_model=IncidentCreated)
    def create_incident(incident: IncidentCreate, repository: Repository):
        return repository.create(incident)

    @app.get("/incidents", response_model=IncidentPage)
    def list_incidents(
        repository: Repository,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ):
        return {"items": repository.list(limit, offset), "limit": limit, "offset": offset}

    @app.get("/incidents/{incident_id}", response_model=Incident)
    def get_incident(incident_id: Annotated[int, Path(gt=0)], repository: Repository):
        incident = repository.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        return incident

    return app
