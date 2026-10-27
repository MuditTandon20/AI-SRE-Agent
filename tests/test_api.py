from datetime import datetime, timezone
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from ai_sre_agent.app import create_app, get_repository


@pytest.fixture
def client(settings, fake_pool):
    app = create_app(settings, pool_factory=lambda _: fake_pool)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    assert fake_pool.opened and fake_pool.closed


@pytest.fixture
def repository(client):
    repository = Mock()
    client.app.dependency_overrides[get_repository] = lambda: repository
    return repository


def test_liveness_stays_healthy_when_database_is_down(client, caplog):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/ready")
    assert response.status_code == 503
    assert "sensitive database diagnostic" not in response.text
    assert "sensitive database diagnostic" not in caplog.text


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=1000001"])
def test_invalid_pagination_never_queries_database(client, repository, query):
    assert client.get(f"/incidents?{query}").status_code == 422
    repository.list.assert_not_called()


@pytest.mark.parametrize(
    "changes", [{"severity": "critical"}, {"title": "   "}, {"symptom": "x" * 10001}]
)
def test_invalid_incident_never_reaches_repository(client, repository, changes):
    payload = {"title": "Error", "path": "/checkout", "symptom": "Timeout", "severity": "SEV1"}
    payload.update(changes)
    assert client.post("/incidents", json=payload).status_code == 422
    repository.create.assert_not_called()


def test_creation_preserves_existing_response_shape(client, repository):
    repository.create.return_value = {
        "id": 7,
        "status": "open",
        "detected_at": datetime.now(timezone.utc),
    }
    response = client.post(
        "/incidents",
        json={"title": "Error", "path": "/", "symptom": "Failure", "severity": "SEV2"},
    )
    assert response.status_code == 201
    assert set(response.json()) == {"id", "status", "detected_at"}


def test_missing_incident_and_route_metrics(client, repository):
    repository.get.return_value = None
    assert client.get("/incidents/123").status_code == 404
    assert client.get("/incidents/456").status_code == 404
    assert client.get("/incidents/0").status_code == 422
    metrics = client.get("/metrics").text
    assert 'route="/incidents/{incident_id}"' in metrics
    assert 'route="/incidents/123"' not in metrics


def test_crashes_are_counted_and_unmatched_routes_are_bounded(client):
    assert client.get("/crash").status_code == 500
    assert client.get("/not-a-real-route").status_code == 404
    metrics = client.get("/metrics").text
    assert 'route="/crash",status="500"' in metrics
    assert 'route="unmatched",status="404"' in metrics


def test_database_failure_returns_sanitized_service_unavailable(client, repository):
    repository.list.side_effect = psycopg.OperationalError("password=private")
    response = client.get("/incidents")
    assert response.status_code == 503
    assert "private" not in response.text


def test_failed_startup_still_closes_pool(settings, fake_pool):
    fake_pool.error = PoolTimeout("database not ready")
    app = create_app(settings, pool_factory=lambda _: fake_pool)
    with pytest.raises(PoolTimeout), TestClient(app):
        pass
    assert fake_pool.closed


def test_public_docs_describe_authentication_without_exposing_data(settings, fake_pool):
    settings.execution_mode = "live"
    with TestClient(create_app(settings, pool_factory=lambda _: fake_pool)) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        assert schema["paths"]["/incidents"]["get"]["security"] == [{"BearerAuth": []}]
        assert client.get("/incidents").status_code == 503
