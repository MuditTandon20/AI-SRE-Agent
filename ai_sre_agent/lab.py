"""Lab controls and concurrent traffic: python -m ai_sre_agent.lab --help."""

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["traffic", "scenario", "reset"])
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--scenario", default="application_error")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error("seconds must be 1–600")
    if args.command != "traffic":
        token = os.environ.get("DEMO_CONTROL_TOKEN")
        if not token:
            parser.error("Set DEMO_CONTROL_TOKEN")
        response = httpx.post(
            args.url + "/scenario",
            headers={"Authorization": "Bearer " + token},
            json={"scenario": "healthy" if args.command == "reset" else args.scenario},
            timeout=5,
        )
        response.raise_for_status()
        print(response.text)
        return
    end = time.monotonic() + args.seconds
    with httpx.Client(timeout=5) as client, ThreadPoolExecutor(max_workers=8) as executor:

        def request(_):
            try:
                return client.get(args.url + "/checkout").status_code
            except httpx.HTTPError:
                return "unavailable"

        while time.monotonic() < end:
            statuses = list(executor.map(request, range(8)))
            print({str(code): statuses.count(code) for code in set(statuses)})
            time.sleep(0.25)


if __name__ == "__main__":
    main()
