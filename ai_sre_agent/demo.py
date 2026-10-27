"""Separate synthetic incident lab. Never run against production workloads."""

import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

from ai_sre_agent.domain import utcnow


class Scenario(BaseModel):
    scenario: Literal[
        "healthy",
        "application_error",
        "downstream_timeout",
        "database_pool",
        "configuration_error",
        "ambiguous",
    ]


def create_demo():
    lock = Lock()
    registry = CollectorRegistry()
    requests = Counter(
        "demo_requests_total", "Demo checkout requests", ["service", "code"], registry=registry
    )
    latency = Histogram(
        "demo_duration_seconds", "Demo checkout latency", ["service"], registry=registry
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.scenario = "healthy"
        root = Path(os.environ.get("DEMO_LOG_DIR", ".demo/logs"))
        root.mkdir(parents=True, exist_ok=True)
        app.state.log_path = root / "demo.jsonl"
        app.state.pool = None
        url = os.environ.get("DEMO_DATABASE_URL")
        if url:
            from psycopg_pool import ConnectionPool

            app.state.pool = ConnectionPool(url, min_size=1, max_size=2, timeout=0.05)
        elif os.environ.get("POSTGRES_DB"):
            from psycopg_pool import ConnectionPool

            from ai_sre_agent.config import Settings

            app.state.pool = ConnectionPool(
                kwargs=Settings.from_env().connection_kwargs(), min_size=1, max_size=2, timeout=0.05
            )
        try:
            yield
        finally:
            if app.state.pool:
                app.state.pool.close()

    app = FastAPI(title="Synthetic incident lab", lifespan=lifespan)

    def control(request):
        token = os.environ.get("DEMO_CONTROL_TOKEN")
        if not token:
            raise HTTPException(503, "Set DEMO_CONTROL_TOKEN to enable lab controls")
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "Invalid demo control credentials")

    @app.get("/health")
    def health():
        return {"status": "ok", "synthetic": True}

    @app.get("/scenario")
    def scenario(request: Request):
        control(request)
        return {"scenario": request.app.state.scenario, "synthetic": True}

    @app.post("/scenario")
    def change(body: Scenario, request: Request):
        control(request)
        with lock:
            request.app.state.scenario = body.scenario
        return {"scenario": body.scenario, "synthetic": True}

    @app.get("/checkout")
    def checkout(request: Request):
        start = time.perf_counter()
        state = request.app.state.scenario
        code, message = 200, "INFO checkout completed"
        if state == "application_error":
            code, message = 500, "ERROR UnhandledApplicationError synthetic checkout ValueError"
        elif state == "downstream_timeout":
            time.sleep(0.3)
            code, message = (
                504,
                "ERROR DownstreamTimeout dependency=synthetic-payments elapsed_ms=300",
            )
        elif state == "database_pool":
            if request.app.state.pool:
                try:
                    with request.app.state.pool.connection() as conn:
                        conn.execute("SELECT pg_sleep(0.25)")
                except Exception:
                    code, message = (
                        503,
                        "ERROR PoolAcquisitionTimeout pool_max=2 synthetic_lab=true",
                    )
            else:
                code, message = 503, "ERROR PoolAcquisitionTimeout simulated_pool_no_database=true"
        elif state == "configuration_error":
            code, message = 503, "ERROR ConfigurationValidationError synthetic PAYMENT_URL missing"
        elif state == "ambiguous":
            time.sleep(0.15)
            message = "WARN elevated latency; causal telemetry unavailable"
        requests.labels("demo-service", str(code)).inc()
        latency.labels("demo-service").observe(time.perf_counter() - start)
        trace_id = None
        try:
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            if context.is_valid:
                trace_id = format(context.trace_id, "032x")
        except ImportError:
            pass
        entry = {
            "timestamp": utcnow().isoformat(),
            "service": "demo-service",
            "environment": "local",
            "scenario": state,
            "message": message,
            "status": code,
            "trace_id": trace_id,
            "synthetic": True,
        }
        with lock, request.app.state.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
        return Response(json.dumps(entry), status_code=code, media_type="application/json")

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    if os.environ.get("DEMO_OTEL_ENABLED") == "true":
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "demo-service"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
    return app


app = create_demo()
