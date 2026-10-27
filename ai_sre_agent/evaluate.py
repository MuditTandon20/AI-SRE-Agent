"""Repeatable synthetic baseline; optional live provider comparisons are explicit."""

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from ai_sre_agent.config import Settings
from ai_sre_agent.connectors import Connectors
from ai_sre_agent.providers import JevDecisionAdapter, OpenAIProvider, deterministic_analysis
from ai_sre_agent.scenarios import expected_outcome, scenario_names


def evaluate(settings, repeats=1, include_jev=False):
    rows = []
    for name in scenario_names():
        for run in range(repeats):
            incident = {
                "id": 1,
                "title": "Synthetic incident",
                "symptom": "Service degradation",
                "service": "demo-service",
                "environment": "local",
            }
            evidence = Connectors(settings).fixture(incident, name)
            started = time.perf_counter()
            if settings.llm_provider == "openai":
                analysis, usage = OpenAIProvider(settings).analyze(incident, evidence)
            else:
                analysis, usage = deterministic_analysis(evidence), {}
            decision = None
            if include_jev:
                decision = JevDecisionAdapter(settings).decide(
                    "evidence_sufficiency",
                    ["sufficient", "insufficient"],
                    {
                        "hypotheses": [h.model_dump() for h in analysis.hypotheses],
                        "evidence": [e.model_dump(mode="json") for e in evidence],
                    },
                )
            actual = analysis.hypotheses[0].cause if analysis.hypotheses else None
            expected = expected_outcome(name)
            ids = {e.id for e in evidence}
            valid_citations = all(
                set(h.supporting_evidence + h.contradicting_evidence) <= ids
                for h in analysis.hypotheses
            )
            rows.append(
                {
                    "scenario": name,
                    "run": run,
                    "expected": expected,
                    "actual": actual,
                    "correct": actual == expected,
                    "valid_citations": valid_citations,
                    "abstained": analysis.outcome == "insufficient_evidence",
                    "seconds": time.perf_counter() - started,
                    "usage": usage,
                    "decision": decision,
                }
            )
    durations = sorted(row["seconds"] for row in rows)
    positive = [row for row in rows if row["expected"] is not None]
    negative = [row for row in rows if row["expected"] is None]
    return {
        "dataset": "synthetic-v1",
        "provider": settings.llm_provider,
        "cases": len(rows),
        "top1_accuracy_nonambiguous": sum(x["correct"] for x in positive) / len(positive),
        "correct_abstention_rate": sum(x["abstained"] for x in negative) / len(negative),
        "citation_validity": sum(x["valid_citations"] for x in rows) / len(rows),
        "latency_p50_seconds": statistics.median(durations),
        "latency_p95_seconds": durations[min(len(durations) - 1, int(len(durations) * 0.95))],
        "manual_mtti_baseline": None,
        "cost_usd": None,
        "limitations": [
            "Synthetic signature fixtures are not a held-out real-world accuracy benchmark",
            "Citation validity does not measure causal support",
            "No measured manual MTTI comparison",
        ],
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/benchmark.json")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--jev", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5:
        parser.error("repeats must be 1–5")
    settings = Settings(
        postgres_db="unused",
        postgres_user="unused",
        postgres_password="unused",
        llm_provider="openai" if args.live_model else "deterministic",
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-6.1-sol"),
        typesafe_api_key=os.environ.get("TYPESAFE_API_KEY"),
    )
    result = evaluate(settings, args.repeats, args.jev)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
