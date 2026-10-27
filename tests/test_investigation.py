from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai_sre_agent.app import create_app
from ai_sre_agent.domain import Alert, Evidence, Report, utcnow
from ai_sre_agent.migrate import apply_migrations
from ai_sre_agent.notifications import dispatch_once
from ai_sre_agent.safety import BudgetExceeded
from ai_sre_agent.store import LeaseLost, Store
from ai_sre_agent.worker import run_once

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(settings, database_pool):
    pool, conn = database_pool
    apply_migrations(conn)
    settings.notification_mode = "fixture"
    with TestClient(create_app(settings, pool_factory=lambda _: pool)) as client:
        yield client, Store(pool), conn, settings


def create_incident(client):
    return client.post(
        "/incidents",
        json={
            "title": "Checkout error",
            "path": "/checkout",
            "symptom": "Database timeout",
            "severity": "SEV1",
        },
    ).json()["id"]


def start(client, incident_id, scenario="database_pool"):
    response = client.post(f"/incidents/{incident_id}/investigations", json={"scenario": scenario})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_end_to_end_report_history_notifications(runtime):
    client, store, conn, settings = runtime
    incident_id = create_incident(client)
    investigation_id = start(client, incident_id)
    assert run_once(store, settings)
    report = client.get(f"/investigations/{investigation_id}/report").json()
    assert report["analysis"]["hypotheses"][0]["cause"] == "database pool exhaustion"
    assert report["mode"] == "fixture" and report["evidence"][0]["synthetic"]
    assert report["usage"]["tool_calls"] >= 1
    assert Report.model_validate(report)
    assert (
        "database pool exhaustion"
        in client.get(f"/investigations/{investigation_id}/report?format=markdown").text
    )
    assert client.get(f"/investigations/{investigation_id}/evidence").json()["items"]
    assert client.get(f"/incidents/{incident_id}").json()["status"] == "open"
    assert len(client.get(f"/incidents/{incident_id}/timeline").json()["items"]) >= 4
    assert dispatch_once(store, settings)
    assert not dispatch_once(store, settings)
    assert client.get("/notifications").json()["items"][0]["state"] == "accepted"
    document_id = client.post(f"/investigations/{investigation_id}/archive").json()["id"]
    assert client.get("/history?service=demo-service&environment=local&q=telemetry").json()["items"]
    assert not client.get("/history?service=other&environment=local&q=telemetry").json()["items"]
    assert client.delete(f"/history/{document_id}").status_code == 200


def test_ambiguous_and_contradictory_incidents_abstain(runtime):
    client, store, _, settings = runtime
    for scenario in ["ambiguous", "database_pool_missing", "database_pool_conflicting"]:
        investigation_id = start(client, create_incident(client), scenario)
        run_once(store, settings)
        report = store.report(investigation_id)
        assert report["analysis"]["outcome"] == "insufficient_evidence"
        assert report["analysis"]["missing_evidence"]


def test_concurrent_duplicate_webhooks_enqueue_once(runtime):
    client, store, conn, settings = runtime
    alert = Alert(
        status="firing",
        labels={"alertname": "Error", "service": "demo-service", "severity": "critical"},
        startsAt=datetime.now(timezone.utc),
        fingerprint="same-alert",
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store.ingest_alerts([alert], settings), range(4)))
    ids = {result[0]["incident_id"] for result in results}
    assert len(ids) == 1
    assert conn.execute("SELECT count(*) FROM investigations").fetchone()[0] == 1
    alert.status = "resolved"
    store.ingest_alerts([alert], settings)
    assert client.get(f"/incidents/{next(iter(ids))}").json()["status"] == "open"
    # Same fingerprint, new occurrence timestamp: a distinct incident.
    alert.status = "firing"
    alert.startsAt = utcnow()
    store.ingest_alerts([alert], settings)
    assert conn.execute("SELECT count(*) FROM investigations").fetchone()[0] == 2


def test_active_investigation_deduplication_cancellation_and_fencing(runtime):
    client, store, conn, settings = runtime
    incident_id = create_incident(client)
    first = start(client, incident_id)
    assert start(client, incident_id) == first
    job = store.claim(settings)
    assert client.post(f"/investigations/{first}/cancel").status_code == 200
    with pytest.raises(LeaseLost):
        store.checkpoint(job, "collect", {})
    assert start(client, incident_id) != first


def test_expired_lease_recovery_and_old_worker_cannot_write(runtime):
    client, store, conn, settings = runtime
    investigation_id = start(client, create_incident(client))
    old = store.claim(settings)
    conn.execute(
        "UPDATE investigations SET lease_until=now()-interval '1 second' WHERE id=%s",
        (investigation_id,),
    )
    replacement = store.claim(settings)
    assert replacement["attempt"] == 2
    assert replacement["lease_token"] != old["lease_token"]
    with pytest.raises(LeaseLost):
        store.checkpoint(old, "collect", {})
    store.checkpoint(replacement, "collect", {"evidence": [], "limitations": ["recovered"]})


def test_persistent_budget_and_queue_backpressure(runtime):
    client, store, conn, settings = runtime
    settings.max_tool_calls = 1
    settings.queue_limit = 1
    start(client, create_incident(client))
    second_id = create_incident(client)
    assert client.post(f"/incidents/{second_id}/investigations", json={}).status_code == 429
    job = store.claim(settings)
    store.reserve(job, "tool", settings)
    with pytest.raises(BudgetExceeded):
        store.reserve(job, "tool", settings)
    conn.execute("UPDATE investigations SET lease_until=now()-interval '1 second'")
    replacement = store.claim(settings)
    with pytest.raises(BudgetExceeded):
        store.reserve(replacement, "tool", settings)


def test_checkpoint_recovery_does_not_repeat_collected_tools(runtime):
    client, store, conn, settings = runtime
    investigation_id = start(client, create_incident(client))
    job = store.claim(settings)
    evidence = Evidence(
        source="fixture:logs",
        kind="logs",
        observed_at=utcnow(),
        query="replayed",
        resource="demo-service",
        content="ERROR PoolAcquisitionTimeout",
        synthetic=True,
    )
    store.checkpoint(
        job, "collect", {"evidence": [evidence.model_dump(mode="json")], "limitations": []}
    )
    conn.execute("UPDATE investigations SET lease_until=now()-interval '1 second'")
    run_once(store, settings)
    report = store.report(investigation_id)
    assert report["evidence"][0]["id"] == evidence.id
    assert report["usage"].get("tool_calls", 0) == 0


def test_grouped_webhook_validation_and_atomicity(runtime):
    client, store, conn, settings = runtime
    invalid = {
        "alerts": [
            {
                "status": "firing",
                "labels": {},
                "startsAt": "2026-10-27T00:00:00Z",
                "fingerprint": "x",
            }
        ]
    }
    assert client.post("/webhooks/alertmanager", json=invalid).status_code == 422
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0


def test_notification_retries_are_bounded(runtime):
    client, store, conn, settings = runtime
    settings.notification_mode = "live"
    settings.ses_recipient = "test@example.invalid"
    start(client, create_incident(client))
    run_once(store, settings)

    class FailingSender:
        def send(self, *args):
            raise RuntimeError("password=private")

    for _ in range(3):
        assert dispatch_once(store, settings, FailingSender())
        conn.execute("UPDATE notification_outbox SET next_attempt=now()")
    assert not dispatch_once(store, settings, FailingSender())
    row = client.get("/notifications").json()["items"][0]
    assert row["state"] == "failed" and row["attempt"] == 3
    assert "private" not in row["error"]


def test_delivery_callback_requires_independent_role_and_provider_correlation(runtime):
    from pydantic import SecretStr

    client, store, conn, settings = runtime
    settings.delivery_token = SecretStr("delivery-only-test-token")
    start(client, create_incident(client))
    run_once(store, settings)
    dispatch_once(store, settings)
    item = client.get("/notifications").json()["items"][0]
    url = "/webhooks/delivery/" + item["id"]
    body = {"provider_id": item["provider_id"], "state": "delivered"}
    assert client.post(url, json=body).status_code == 401
    headers = {"Authorization": "Bearer delivery-only-test-token"}
    assert (
        client.post(url, headers=headers, json={**body, "provider_id": "wrong"}).status_code == 409
    )
    assert client.post(url, headers=headers, json=body).status_code == 200
    assert client.post(url, headers=headers, json=body).status_code == 200
    assert client.get("/notifications").json()["items"][0]["state"] == "delivered"


def test_severity_threshold_controls_email_routing(runtime):
    client, store, conn, settings = runtime
    settings.notification_mode = "live"
    settings.ses_recipient = "test@example.invalid"
    settings.ses_min_severity = "SEV1"
    incident = client.post(
        "/incidents",
        json={"title": "Low-priority event", "path": "/", "symptom": "Slow", "severity": "SEV3"},
    ).json()["id"]
    start(client, incident)
    run_once(store, settings)
    assert conn.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0


def test_invalid_model_citations_produce_insufficient_evidence(runtime, monkeypatch):
    from ai_sre_agent.domain import Analysis, Hypothesis

    client, store, conn, settings = runtime
    settings.llm_provider = "openai"

    class InvalidModel:
        def __init__(self, *args):
            pass

        def analyze(self, *args):
            return Analysis(
                summary="Unsupported",
                outcome="hypotheses",
                hypotheses=[
                    Hypothesis(
                        cause="invented",
                        reasoning="unsupported",
                        supporting_evidence=["fabricated-id"],
                        contradicting_evidence=[],
                        confidence="high",
                    )
                ],
                missing_evidence=[],
                recommendations=[],
            ), {}

    monkeypatch.setattr("ai_sre_agent.workflow.OpenAIProvider", InvalidModel)
    investigation_id = start(client, create_incident(client))
    run_once(store, settings)
    report = store.report(investigation_id)
    assert report["analysis"]["outcome"] == "insufficient_evidence"
    assert report["usage"]["model_calls"] == 1
