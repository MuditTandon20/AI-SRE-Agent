"""Opt-in bounded API read load test; never sends writes or prints credentials."""

import argparse
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8010")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.requests <= 1000 or not 1 <= args.concurrency <= 16:
        parser.error("requests must be 1–1000 and concurrency 1–16")
    headers = {"Authorization": "Bearer " + os.environ.get("API_TOKEN", "")}
    with (
        httpx.Client(timeout=10, headers=headers, trust_env=False) as client,
        ThreadPoolExecutor(max_workers=args.concurrency) as executor,
    ):

        def request(_):
            start = time.perf_counter()
            try:
                status = client.get(args.url + "/incidents?limit=1").status_code
            except httpx.HTTPError:
                status = 0
            return status, time.perf_counter() - start

        result = list(executor.map(request, range(args.requests)))
    durations = sorted(x[1] for x in result)
    print(
        json.dumps(
            {
                "requests": len(result),
                "successes": sum(x[0] == 200 for x in result),
                "p50_seconds": statistics.median(durations),
                "p95_seconds": durations[min(len(result) - 1, int(len(result) * 0.95))],
            }
        )
    )


if __name__ == "__main__":
    main()
