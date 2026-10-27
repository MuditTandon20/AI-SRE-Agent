"""Immutable proposals and approvals, separate from action execution."""

from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from ai_sre_agent.connectors import BoundedHTTP, ConnectorError
from ai_sre_agent.domain import utcnow
from ai_sre_agent.safety import authorize, configured_url, digest
from ai_sre_agent.store import Store

router = APIRouter(prefix="/remediation")


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: int = Field(gt=0)
    action: Literal["reset_demo"] = "reset_demo"
    target: Literal["demo-service"] = "demo-service"
    environment: Literal["local"] = "local"
    expected_scenario: str = Field(min_length=1, max_length=100)
    expires_in_seconds: int = Field(default=300, ge=30, le=900)


class ApprovalRequest(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.post("/proposals", status_code=201)
def propose(body: ProposalRequest, request: Request):
    authorize(request)
    if not request.app.state.settings.remediation_enabled:
        raise HTTPException(403, "Remediation is disabled")
    proposal_id = uuid4()
    content = {
        **body.model_dump(),
        "id": str(proposal_id),
        "expires_at": (utcnow() + timedelta(seconds=body.expires_in_seconds)).isoformat(),
    }
    checksum = digest(content)
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "SELECT service,environment FROM incidents WHERE id=%s", (body.incident_id,)
        ).fetchone()
        if row != (body.target, body.environment):
            raise HTTPException(422, "Incident is outside local demo scope")
        conn.execute(
            "INSERT INTO remediation_proposals (id,incident_id,body,digest,expires_at) VALUES (%s,%s,%s,%s,%s)",
            (proposal_id, body.incident_id, Jsonb(content), checksum, content["expires_at"]),
        )
        Store.event(
            conn.cursor(),
            body.incident_id,
            "remediation.proposed",
            {"proposal_id": str(proposal_id), "digest": checksum},
        )
    return {
        "id": proposal_id,
        "digest": checksum,
        "body": content,
        "state": "proposed",
        "risk": "Local demo reset only",
        "verification": "Scenario becomes healthy",
        "containment": "Stop and require manual review if verification fails",
    }


@router.get("/proposals/{proposal_id}")
def proposal(proposal_id: UUID, request: Request):
    authorize(request)
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT * FROM remediation_proposals WHERE id=%s", (proposal_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Proposal not found")
        return row


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: UUID, body: ApprovalRequest, request: Request):
    actor = authorize(request, "approver")
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT * FROM remediation_proposals WHERE id=%s FOR UPDATE", (proposal_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Proposal not found")
        if (
            row["state"] != "proposed"
            or row["expires_at"] <= utcnow()
            or row["digest"] != body.digest
            or digest(row["body"]) != body.digest
        ):
            raise HTTPException(409, "Proposal changed, expired or is no longer awaiting approval")
        cur.execute(
            "UPDATE remediation_proposals SET state='approved',approved_digest=%s,approved_by=%s,approved_at=now() WHERE id=%s",
            (body.digest, actor, proposal_id),
        )
        Store.event(
            cur,
            row["incident_id"],
            "remediation.approved",
            {"proposal_id": str(proposal_id), "actor": actor},
        )
    return {"state": "approved"}


@router.post("/proposals/{proposal_id}/revoke")
def revoke(proposal_id: UUID, request: Request):
    authorize(request, "approver")
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "UPDATE remediation_proposals SET state='revoked' WHERE id=%s AND state IN ('proposed','approved') RETURNING incident_id",
            (proposal_id,),
        ).fetchone()
        if not row:
            raise HTTPException(409, "Proposal cannot be revoked")
        Store.event(conn.cursor(), row[0], "remediation.revoked", {"proposal_id": str(proposal_id)})
    return {"state": "revoked"}


@router.post("/proposals/{proposal_id}/execute")
def execute(proposal_id: UUID, request: Request):
    authorize(request)
    settings = request.app.state.settings
    if not settings.remediation_enabled or not settings.executor_url or not settings.executor_token:
        raise HTTPException(403, "Separate executor is not configured")
    try:
        return BoundedHTTP(10).json(
            "POST",
            configured_url(settings.executor_url) + f"/execute/{proposal_id}",
            headers={"Authorization": "Bearer " + settings.executor_token.get_secret_value()},
            body={},
        )
    except ConnectorError:
        raise HTTPException(
            502, "Execution not confirmed; inspect proposal state before retry"
        ) from None
