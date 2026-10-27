import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ai_sre_agent.app import create_app
from ai_sre_agent.executor import create_executor
from ai_sre_agent.migrate import apply_migrations

pytestmark = pytest.mark.integration


@pytest.fixture
def remediation_runtime(settings, database_pool, monkeypatch):
    pool, conn = database_pool
    apply_migrations(conn)
    settings.remediation_enabled = True
    settings.api_token = SecretStr("operator-test-token")
    settings.approver_token = SecretStr("approver-test-token")
    settings.executor_token = SecretStr("executor-test-token")
    monkeypatch.setenv("DEMO_CONTROL_TOKEN", "demo-control-test-token")

    class DemoControl:
        def __init__(self):
            self.scenario = "database_pool"
            self.writes = 0

        def json(self, method, url, **kwargs):
            if method == "POST":
                self.writes += 1
                self.scenario = kwargs["body"]["scenario"]
            return {"scenario": self.scenario}

    demo = DemoControl()
    operator_headers = {"Authorization": "Bearer operator-test-token"}
    approver_headers = {"Authorization": "Bearer approver-test-token"}
    executor_headers = {"Authorization": "Bearer executor-test-token"}

    # Both apps share a test pool; close only after both contexts exit.
    class SharedPool:
        def open(self, **kwargs):
            pool.open(**kwargs)

        def close(self):
            pass

        def connection(self):
            return pool.connection()

    shared = SharedPool()
    with (
        TestClient(create_app(settings, pool_factory=lambda _: shared)) as api,
        TestClient(create_executor(settings, pool_factory=lambda _: shared, http=demo)) as executor,
    ):
        incident = api.post(
            "/incidents",
            headers=operator_headers,
            json={"title": "Pool issue", "path": "/", "symptom": "Error", "severity": "SEV1"},
        ).json()["id"]
        response = api.post(
            "/remediation/proposals",
            headers=operator_headers,
            json={"incident_id": incident, "expected_scenario": "database_pool"},
        )
        assert response.status_code == 201, response.text
        yield (
            api,
            executor,
            conn,
            response.json(),
            demo,
            operator_headers,
            approver_headers,
            executor_headers,
        )


def test_action_requires_approval_and_has_idempotent_completed_result(remediation_runtime):
    api, executor, conn, proposal, demo, operator, approver, execution = remediation_runtime
    url = "/execute/" + proposal["id"]
    assert executor.post(url, headers=execution).status_code == 409
    approval_url = "/remediation/proposals/" + proposal["id"] + "/approve"
    assert (
        api.post(approval_url, headers=operator, json={"digest": proposal["digest"]}).status_code
        == 401
    )
    assert (
        api.post(approval_url, headers=approver, json={"digest": proposal["digest"]}).status_code
        == 200
    )
    assert executor.post(url, headers=operator).status_code == 401
    result = executor.post(url, headers=execution)
    assert result.status_code == 200 and result.json()["verified"]
    assert executor.post(url, headers=execution).json() == result.json()
    assert demo.writes == 1


@pytest.mark.parametrize("invalidity", ["expired", "tampered", "revoked", "precondition"])
def test_approval_cannot_authorize_changed_expired_or_wrong_state_action(
    remediation_runtime, invalidity
):
    api, executor, conn, proposal, demo, operator, approver, execution = remediation_runtime
    base = "/remediation/proposals/" + proposal["id"]
    assert (
        api.post(
            base + "/approve", headers=approver, json={"digest": proposal["digest"]}
        ).status_code
        == 200
    )
    if invalidity == "expired":
        conn.execute(
            "UPDATE remediation_proposals SET expires_at=now()-interval '1 second' WHERE id=%s",
            (proposal["id"],),
        )
    elif invalidity == "tampered":
        conn.execute(
            "UPDATE remediation_proposals SET body=jsonb_set(body,'{target}','\"production\"') WHERE id=%s",
            (proposal["id"],),
        )
    elif invalidity == "revoked":
        assert api.post(base + "/revoke", headers=approver).status_code == 200
    else:
        demo.scenario = "healthy"
    assert executor.post("/execute/" + proposal["id"], headers=execution).status_code == 409
    assert demo.writes == 0


def test_wrong_approval_digest_and_nonlocal_target_are_rejected(remediation_runtime):
    api, executor, conn, proposal, demo, operator, approver, execution = remediation_runtime
    assert (
        api.post(
            "/remediation/proposals/" + proposal["id"] + "/approve",
            headers=approver,
            json={"digest": "0" * 64},
        ).status_code
        == 409
    )
    assert (
        api.post(
            "/remediation/proposals",
            headers=operator,
            json={"incident_id": 1, "target": "production", "expected_scenario": "error"},
        ).status_code
        == 422
    )
