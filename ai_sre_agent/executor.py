"""Independent executor; only an approved, allowlisted local demo reset is supported."""

import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_sre_agent.config import Settings
from ai_sre_agent.connectors import BoundedHTTP
from ai_sre_agent.database import create_pool
from ai_sre_agent.domain import utcnow
from ai_sre_agent.safety import configured_url, digest
from ai_sre_agent.store import Store


def create_executor(settings=None, pool_factory=create_pool, http=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or Settings.from_env()
        pool = pool_factory(app.state.settings)
        app.state.pool = pool
        try:
            await run_in_threadpool(pool.open, wait=True, timeout=10)
            yield
        finally:
            await run_in_threadpool(pool.close)

    app = FastAPI(title="AI SRE local-lab executor", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "scope": "local-demo-only"}

    @app.post("/execute/{proposal_id}")
    def execute(proposal_id: UUID, request: Request):
        configuration = request.app.state.settings
        token = configuration.executor_token
        if not configuration.remediation_enabled or token is None:
            raise HTTPException(403, "Executor is disabled")
        if not secrets.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + token.get_secret_value()
        ):
            raise HTTPException(401, "Invalid executor credentials")
        with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT * FROM remediation_proposals WHERE id=%s FOR UPDATE", (proposal_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Proposal not found")
            if row["state"] == "completed":
                return row["result"]
            if (
                row["state"] != "approved"
                or row["expires_at"] <= utcnow()
                or not row["approved_by"]
                or row["approved_digest"] != row["digest"]
                or digest(row["body"]) != row["digest"]
            ):
                raise HTTPException(409, "No valid current approval")
            body = row["body"]
            if datetime.fromisoformat(body["expires_at"]) != row["expires_at"]:
                raise HTTPException(409, "Proposal expiry metadata changed")
            if (
                body.get("action") != "reset_demo"
                or body.get("target") != "demo-service"
                or body.get("environment") != "local"
            ):
                raise HTTPException(403, "Action is outside executor policy")
            cur.execute(
                "UPDATE remediation_proposals SET state='executing' WHERE id=%s", (proposal_id,)
            )
            Store.event(
                cur, row["incident_id"], "remediation.executing", {"proposal_id": str(proposal_id)}
            )
        try:
            base = configured_url(os.environ.get("DEMO_CONTROL_URL", "http://localhost:8001"))
            control_token = os.environ.get("DEMO_CONTROL_TOKEN")
            if not control_token:
                raise ValueError("Demo control credentials missing")
            headers = {"Authorization": "Bearer " + control_token}
            client = http or BoundedHTTP(5)
            before = client.json("GET", base + "/scenario", headers=headers)
            if before.get("scenario") != body["expected_scenario"]:
                raise ValueError("Precondition changed")
            client.json("POST", base + "/scenario", headers=headers, body={"scenario": "healthy"})
            if client.json("GET", base + "/scenario", headers=headers).get("scenario") != "healthy":
                raise ValueError("Post-action verification failed")
            result = {
                "state": "completed",
                "verified": True,
                "target": "demo-service",
                "environment": "local",
            }
            with request.app.state.pool.connection() as conn:
                conn.execute(
                    "UPDATE remediation_proposals SET state='completed',result=%s WHERE id=%s AND state='executing'",
                    (Jsonb(result), proposal_id),
                )
                Store.event(
                    conn.cursor(),
                    row["incident_id"],
                    "remediation.completed",
                    {"proposal_id": str(proposal_id)},
                )
            return result
        except Exception as exc:
            with request.app.state.pool.connection() as conn:
                conn.execute(
                    "UPDATE remediation_proposals SET state='failed',error=%s WHERE id=%s",
                    (type(exc).__name__, proposal_id),
                )
            raise HTTPException(409, "Action not verified; manual review required") from None

    return app


app = create_executor()
