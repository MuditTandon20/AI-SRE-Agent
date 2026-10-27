"""Developer client for the token-protected local lab."""

import argparse
import time

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["investigate", "scenario", "reset"])
    parser.add_argument("--url", default="http://localhost:8010")
    parser.add_argument("--demo-url", default="http://localhost:8001")
    parser.add_argument("--scenario", default="database_pool")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    values = dotenv_values(".demo/lab.env")
    headers = {"Authorization": "Bearer " + values.get("API_TOKEN", "")}
    with httpx.Client(timeout=10, trust_env=False) as client:
        if args.command in {"scenario", "reset"}:
            response = client.post(
                args.demo_url + "/scenario",
                headers={"Authorization": "Bearer " + values.get("DEMO_CONTROL_TOKEN", "")},
                json={"scenario": "healthy" if args.command == "reset" else args.scenario},
            )
            response.raise_for_status()
            print(response.text)
            return
        response = client.post(
            args.url + "/incidents",
            headers=headers,
            json={
                "title": "Developer investigation",
                "path": "/checkout",
                "symptom": "Checkout error or latency",
                "severity": "SEV2",
            },
        )
        response.raise_for_status()
        incident_id = response.json()["id"]
        response = client.post(
            args.url + f"/incidents/{incident_id}/investigations",
            headers=headers,
            json={} if args.live else {"scenario": args.scenario},
        )
        response.raise_for_status()
        investigation_id = response.json()["id"]
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            response = client.get(args.url + f"/investigations/{investigation_id}", headers=headers)
            response.raise_for_status()
            status = response.json()["status"]
            if status == "completed":
                response = client.get(
                    args.url + f"/investigations/{investigation_id}/report?format=markdown",
                    headers=headers,
                )
                response.raise_for_status()
                print(response.text)
                return
            if status in {"failed", "cancelled"}:
                raise SystemExit("Investigation " + status + "; inspect its audit timeline")
            time.sleep(1)
        raise SystemExit("Timed out waiting for the investigation")


if __name__ == "__main__":
    main()
