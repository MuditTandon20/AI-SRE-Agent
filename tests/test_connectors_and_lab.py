import json

import httpx
from fastapi.testclient import TestClient

from ai_sre_agent.connectors import BoundedHTTP, Connectors
from ai_sre_agent.demo import create_demo
from ai_sre_agent.evaluate import evaluate
from ai_sre_agent.notifications import SES, SlackMCP


def test_metrics_logs_traces_and_infrastructure_normalization(settings):
    settings.tempo_url = "http://tempo"
    settings.kubernetes_url = "https://cluster.internal"
    settings.github_repository = "example/project"
    observed = []

    def handler(request):
        observed.append(request)
        path = request.url.path
        if path.endswith("query_range") and "loki" not in path:
            assert request.url.params["step"] == "15"
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        if "loki" in path:
            assert int(request.url.params["limit"]) <= 100
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "values": [
                                    [
                                        "1793059200000000000",
                                        "ERROR DownstreamTimeout password=private",
                                    ]
                                ]
                            }
                        ]
                    },
                },
            )
        if path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": "a" * 32}]})
        if "/api/traces/" in path:
            return httpx.Response(
                200,
                json={
                    "batches": [
                        {"scopeSpans": [{"spans": [{"spanId": "123", "name": "payments"}]}]}
                    ]
                },
            )
        if path.endswith("/pods"):
            assert request.url.params["labelSelector"] == "app=demo-service"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "demo-1"},
                            "status": {
                                "phase": "Failed",
                                "containerStatuses": [{"reason": "OOMKilled"}],
                            },
                        }
                    ]
                },
            )
        if path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "abc",
                        "commit": {"message": "Change"},
                        "html_url": "https://github.com/example/project/commit/abc",
                    }
                ],
            )
        raise AssertionError(path)

    connectors = Connectors(settings, BoundedHTTP(transport=httpx.MockTransport(handler)))
    incident = {"service": "demo-service", "environment": "local"}
    assert connectors.prometheus(incident)[0].kind == "metrics"
    log = connectors.loki(incident)[0]
    assert "private" not in log.content
    assert log.observed_at.year == 2026
    assert len(connectors.tempo(incident)) == 2
    assert "OOMKilled" in connectors.kubernetes(incident)[0].content
    assert "correlation only" in connectors.github(incident)[0].query
    assert all(request.method == "GET" for request in observed)


def test_slack_mcp_handshake_and_restricted_destination(settings):
    settings.slack_mcp_url = "https://mcp.example.test/mcp"
    settings.slack_channel = "test-channel"
    methods = []

    def handler(request):
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session1"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}},
            )
        assert request.headers["Mcp-Session-Id"] == "session1"
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert body["params"]["arguments"]["channel"] == "test-channel"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": []}})

    sender = SlackMCP(settings, BoundedHTTP(transport=httpx.MockTransport(handler)))
    assert sender.send("test-channel", "Synthetic report", "1") == "mcp-accepted:1"
    assert methods == ["initialize", "notifications/initialized", "tools/call"]


def test_ses_acceptance_is_not_delivery(settings):
    settings.ses_sender = "sender@example.invalid"
    settings.ses_recipient = "recipient@example.invalid"

    class FakeSES:
        def send_email(self, **kwargs):
            assert kwargs["Destination"]["ToAddresses"] == [settings.ses_recipient]
            return {"MessageId": "accepted-id"}

    assert SES(settings, FakeSES()).send(settings.ses_recipient, "Report", "1") == "accepted-id"


def test_lab_failure_reset_and_log_output(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_CONTROL_TOKEN", "local-test-token")
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.delenv("DEMO_DATABASE_URL", raising=False)
    with TestClient(create_demo()) as client:
        assert client.post("/scenario", json={"scenario": "application_error"}).status_code == 401
        headers = {"Authorization": "Bearer local-test-token"}
        assert (
            client.post(
                "/scenario", headers=headers, json={"scenario": "application_error"}
            ).status_code
            == 200
        )
        assert client.get("/checkout").status_code == 500
        assert "UnhandledApplicationError" in (tmp_path / "demo.jsonl").read_text()
        client.post("/scenario", headers=headers, json={"scenario": "healthy"})
        assert client.get("/checkout").status_code == 200


def test_benchmark_reports_its_synthetic_limitations(settings):
    result = evaluate(settings)
    assert result["cases"] == 30
    assert result["citation_validity"] == 1
    assert result["manual_mtti_baseline"] is None
    assert result["limitations"]
