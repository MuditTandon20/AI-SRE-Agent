"""Explicit synthetic-failure smoke test for the isolated Compose lab.

Starts application-error traffic, checks telemetry/alert/report, then resets.
Requires API/worker EXECUTION_MODE=live and generated .demo/lab.env credentials.
"""

import json
import threading
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    values = dotenv_values(".demo/lab.env")
    api_headers = {"Authorization": "Bearer " + values["API_TOKEN"]}
    control_headers = {"Authorization": "Bearer " + values["DEMO_CONTROL_TOKEN"]}
    stop = threading.Event()
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get("http://localhost:8010/ready")
        response.raise_for_status()
        response = client.post(
            "http://localhost:8001/scenario",
            headers=control_headers,
            json={"scenario": "application_error"},
        )
        response.raise_for_status()

        def traffic():
            with httpx.Client(timeout=5, trust_env=False) as requester:
                while not stop.is_set():
                    try:
                        requester.get("http://localhost:8001/checkout")
                    except httpx.HTTPError:
                        pass
                    stop.wait(0.2)

        threads = [threading.Thread(target=traffic, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            deadline = time.monotonic() + 110
            report = None
            while time.monotonic() < deadline:
                alerts = (
                    client.get("http://localhost:9091/api/v1/alerts")
                    .json()
                    .get("data", {})
                    .get("alerts", [])
                )
                incidents = (
                    client.get("http://localhost:8010/incidents", headers=api_headers)
                    .json()
                    .get("items", [])
                )
                candidates = [item for item in incidents if item["title"] == "Demo checkout errors"]
                for incident in candidates:
                    investigations = client.get(
                        f"http://localhost:8010/incidents/{incident['id']}/investigations",
                        headers=api_headers,
                    )
                    if investigations.status_code != 200:
                        continue
                    for item in investigations.json()["items"]:
                        if item["status"] == "completed" and item["mode"] == "live":
                            response = client.get(
                                f"http://localhost:8010/investigations/{item['id']}/report",
                                headers=api_headers,
                            )
                            report = response.json()
                            break
                if report:
                    break
                print(f"Waiting for alert/report; active_alerts={len(alerts)}", flush=True)
                time.sleep(5)
            if report is None:
                raise RuntimeError("No automatic live investigation completed within deadline")
            assert report["mode"] == "live"
            assert any(item["source"] == "loki" for item in report["evidence"]), report[
                "limitations"
            ]
            assert report["analysis"]["outcome"] == "hypotheses", report["analysis"]
            assert report["analysis"]["hypotheses"][0]["cause"] == "application exception"
            traces = client.get(
                "http://localhost:3200/api/search",
                params={"q": '{ resource.service.name = "demo-service" }', "limit": 5},
            )
            traces.raise_for_status()
            assert traces.json().get("traces"), "No exported demo traces"
            Path("artifacts").mkdir(exist_ok=True)
            Path("artifacts/live-lab-report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            print(
                "PASS: live telemetry -> Prometheus alert -> Alertmanager webhook -> durable investigation -> cited report; Tempo traces present."
            )
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
            response = client.post(
                "http://localhost:8001/scenario",
                headers=control_headers,
                json={"scenario": "healthy"},
            )
            response.raise_for_status()


if __name__ == "__main__":
    main()
