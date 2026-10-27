from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ai_sre_agent.connectors import ConnectorError, Connectors
from ai_sre_agent.domain import AlertWebhook, InvestigationRequest, Report, markdown_report
from ai_sre_agent.safety import authorize
from ai_sre_agent.scenarios import scenario_names
from ai_sre_agent.store import QueueFull, Store

router = APIRouter()


def store(request: Request):
    return Store(request.app.state.pool)


def operator(request: Request):
    return authorize(request)


@router.post("/webhooks/alertmanager", status_code=202)
def webhook(payload: AlertWebhook, request: Request):
    authorize(request, "webhook")
    settings = request.app.state.settings
    try:
        return {"events": store(request).ingest_alerts(payload.alerts, settings)}
    except QueueFull as exc:
        raise HTTPException(429, str(exc)) from None


@router.post(
    "/incidents/{incident_id}/investigations", status_code=202, dependencies=[Depends(operator)]
)
def start_investigation(
    incident_id: Annotated[int, Path(gt=0)], body: InvestigationRequest, request: Request
):
    settings = request.app.state.settings
    if body.scenario and (
        settings.execution_mode != "fixture" or body.scenario not in scenario_names()
    ):
        raise HTTPException(422, "Scenario is only allowed for known fixture cases")
    try:
        row = store(request).enqueue(incident_id, settings, body.scenario)
        return {"id": row["id"], "status": row["status"], "mode": row["mode"]}
    except KeyError:
        raise HTTPException(404, "Incident not found") from None
    except QueueFull as exc:
        raise HTTPException(429, str(exc)) from None


@router.get("/investigations/{investigation_id}", dependencies=[Depends(operator)])
def investigation(investigation_id: UUID, request: Request):
    row = store(request).get_investigation(investigation_id)
    if not row:
        raise HTTPException(404, "Investigation not found")
    return row


@router.post("/investigations/{investigation_id}/cancel", dependencies=[Depends(operator)])
def cancel(investigation_id: UUID, request: Request):
    if not store(request).cancel(investigation_id):
        raise HTTPException(409, "Investigation is absent or already terminal")
    return {"status": "cancelled"}


@router.get("/investigations/{investigation_id}/report", dependencies=[Depends(operator)])
def report(investigation_id: UUID, request: Request, format: Literal["json", "markdown"] = "json"):
    body = store(request).report(investigation_id)
    if body is None:
        raise HTTPException(404, "Report is not available")
    result = Report.model_validate(body)
    return (
        PlainTextResponse(markdown_report(result), media_type="text/markdown")
        if format == "markdown"
        else result
    )


@router.get("/investigations/{investigation_id}/evidence", dependencies=[Depends(operator)])
def evidence(investigation_id: UUID, request: Request):
    body = store(request).report(investigation_id)
    if body is None:
        raise HTTPException(404, "Evidence is not available")
    return {"items": body["evidence"]}


@router.get("/incidents/{incident_id}/timeline", dependencies=[Depends(operator)])
def timeline(incident_id: int, request: Request):
    return {"items": store(request).timeline(incident_id)}


@router.get("/incidents/{incident_id}/investigations", dependencies=[Depends(operator)])
def incident_investigations(incident_id: int, request: Request):
    from psycopg.rows import dict_row

    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return {
            "items": cur.execute(
                "SELECT id,status,mode,created_at,finished_at,error FROM investigations WHERE incident_id=%s ORDER BY created_at DESC LIMIT 100",
                (incident_id,),
            ).fetchall()
        }


class StatusUpdate(BaseModel):
    status: Literal["open", "acknowledged", "resolved"]


@router.patch("/incidents/{incident_id}/status", dependencies=[Depends(operator)])
def status(incident_id: int, body: StatusUpdate, request: Request):
    with request.app.state.pool.connection() as conn, conn.cursor() as cur:
        row = cur.execute(
            "UPDATE incidents SET status=%s WHERE id=%s RETURNING id,status",
            (body.status, incident_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Incident not found")
        Store.event(cur, incident_id, "incident.status", {"status": body.status})
        return {"id": row[0], "status": row[1]}


@router.get("/connectors", dependencies=[Depends(operator)])
def connectors():
    return Connectors.capabilities


@router.get("/connectors/{name}/health", dependencies=[Depends(operator)])
def connector_health(name: str, request: Request):
    try:
        return {"connector": name, "healthy": Connectors(request.app.state.settings).health(name)}
    except (ConnectorError, ValueError):
        raise HTTPException(422, "Connector is absent or not configured") from None


@router.get("/scenarios", dependencies=[Depends(operator)])
def scenarios():
    return {"mode": "synthetic", "items": scenario_names()}


@router.post(
    "/investigations/{investigation_id}/archive", status_code=201, dependencies=[Depends(operator)]
)
def archive(investigation_id: UUID, request: Request):
    try:
        return {
            "id": store(request).archive_report(investigation_id),
            "status": "indexed_as_unverified_history",
        }
    except KeyError:
        raise HTTPException(404, "Report not found") from None


@router.get("/history", dependencies=[Depends(operator)])
def history(
    request: Request,
    service: str,
    environment: str,
    q: Annotated[str, Query(min_length=1, max_length=300)],
):
    return {"items": store(request).history(service, environment, q)}


@router.delete("/history/{document_id}", dependencies=[Depends(operator)])
def delete_history(document_id: UUID, request: Request):
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "DELETE FROM historical_documents WHERE id=%s RETURNING id", (document_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
    return {"deleted": str(document_id)}


@router.get("/notifications", dependencies=[Depends(operator)])
def notifications(request: Request):
    from psycopg.rows import dict_row

    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return {
            "items": cur.execute(
                "SELECT id,investigation_id,channel,destination,mode,state,attempt,provider_id,error FROM notification_outbox ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        }


class DeliveryUpdate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=300)
    state: Literal["delivered", "failed"]


@router.post("/webhooks/delivery/{notification_id}")
def delivery(notification_id: UUID, body: DeliveryUpdate, request: Request):
    authorize(request, "delivery")
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "UPDATE notification_outbox SET state=%s WHERE id=%s AND provider_id=%s AND state='accepted' RETURNING id",
            (body.state, notification_id, body.provider_id),
        ).fetchone()
        if not row:
            previous = conn.execute(
                "SELECT state FROM notification_outbox WHERE id=%s AND provider_id=%s",
                (notification_id, body.provider_id),
            ).fetchone()
            if previous is None or previous[0] != body.state:
                raise HTTPException(
                    409, "Notification/provider correlation is absent or already terminal"
                )
    return {"state": body.state}


@router.post("/notifications/{notification_id}/retry", dependencies=[Depends(operator)])
def retry_notification(notification_id: UUID, request: Request):
    with request.app.state.pool.connection() as conn:
        row = conn.execute(
            "UPDATE notification_outbox SET state='pending',attempt=0,error=NULL,next_attempt=now() WHERE id=%s AND state='failed' RETURNING id",
            (notification_id,),
        ).fetchone()
        if not row:
            raise HTTPException(409, "Only failed notifications may be explicitly retried")
    return {
        "state": "pending",
        "warning": "Reconcile provider acceptance before retrying an ambiguous send",
    }
