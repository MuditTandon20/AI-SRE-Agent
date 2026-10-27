"""Bounded single-orchestrator graph with durable, fenced stage checkpoints."""

import json
from collections import defaultdict, deque
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ai_sre_agent.connectors import ConnectorError, Connectors
from ai_sre_agent.domain import Analysis, Evidence, Report, utcnow
from ai_sre_agent.incidents import IncidentRepository
from ai_sre_agent.providers import (
    JevDecisionAdapter,
    OpenAIProvider,
    RuleDecisionAdapter,
    deterministic_analysis,
    insufficient,
)
from ai_sre_agent.safety import Budget, BudgetExceeded, redact


class InvestigationState(TypedDict, total=False):
    evidence: list[dict]
    limitations: list[str]
    analysis: dict
    provider_usage: dict
    report: dict
    provider: str
    model: str | None


def investigate(job, store, settings, connectors=None):
    if job["mode"] != settings.execution_mode:
        raise ValueError("Worker execution mode differs from queued investigation")
    incident = IncidentRepository(store.pool).get(job["incident_id"])
    connectors = connectors or Connectors(settings)
    remaining = settings.investigation_seconds - (utcnow() - job["started_at"]).total_seconds()
    budget = Budget(
        remaining, settings.max_tool_calls, settings.max_model_calls, dict(job["budget"])
    )

    def call(name, operation):
        budget.remaining()
        store.reserve(job, "tool", settings)
        # A connector cannot receive a timeout longer than the investigation remainder.
        connectors.http.timeout = min(settings.connector_timeout_seconds, budget.remaining())
        try:
            result = operation()
            budget.remaining()
            return result, None
        except (ConnectorError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return [], f"{name}: {type(exc).__name__}; evidence unavailable"

    def collect(state):
        cached = store.load_checkpoint(job, "collect")
        if cached is not None:
            return cached
        evidence, limitations = [], []
        operations = (
            [("fixture", lambda: connectors.fixture(incident, job["scenario"]))]
            if job["mode"] == "fixture"
            else [
                ("prometheus", lambda: connectors.prometheus(incident)),
                ("loki", lambda: connectors.loki(incident)),
            ]
        )
        if job["mode"] == "live" and settings.tempo_url:
            operations.append(("tempo", lambda: connectors.tempo(incident)))
        if job["mode"] == "live" and settings.github_repository:
            operations.append(("github", lambda: connectors.github(incident)))
        if job["mode"] == "live" and settings.kubernetes_url:
            operations.append(("kubernetes", lambda: connectors.kubernetes(incident)))
        operations.append(("runbooks", lambda: connectors.runbooks(incident)))

        def historical_context():
            rows = store.history(
                incident["service"],
                incident["environment"],
                incident["title"] + " " + incident["symptom"],
            )
            return [
                Evidence(
                    source="history:" + str(row["id"]),
                    kind="history",
                    observed_at=row["created_at"],
                    query="scoped keyword retrieval",
                    resource=incident["service"],
                    content="UNVERIFIED HISTORICAL CONTEXT: " + row["content"],
                    source_url=row["source"],
                )
                for row in rows
            ]

        operations.append(("history", historical_context))
        for name, operation in operations:
            try:
                items, error = call(name, operation)
            except BudgetExceeded:
                limitations.append("Collection stopped at configured budget")
                break
            if error:
                limitations.append(error)
            evidence.extend(items)
        # Bound total context, preserving an explicit truncation limitation.
        groups = defaultdict(deque)
        for item in evidence:
            if len(item.content) > settings.max_input_chars // 6:
                item.content = item.content[: settings.max_input_chars // 6]
                item.truncated = True
            groups[item.kind].append(item)
        ordered = []
        while any(groups.values()):
            for kind in (
                "metrics",
                "logs",
                "traces",
                "infrastructure",
                "change",
                "runbook",
                "history",
            ):
                if groups[kind]:
                    ordered.append(groups[kind].popleft())
        kept = []
        size = 0
        for item in ordered:
            item = Evidence.model_validate(redact(item.model_dump(mode="json")))
            length = len(item.model_dump_json())
            if size + length > settings.max_input_chars // 2:
                limitations.append("Evidence context truncated by aggregate limit")
                continue
            size += length
            kept.append(item.model_dump(mode="json"))
        result = {"evidence": kept, "limitations": limitations}
        store.checkpoint(job, "collect", result)
        return result

    def analyze(state):
        cached = store.load_checkpoint(job, "analyze")
        if cached is not None:
            return cached
        evidence = [Evidence.model_validate(x) for x in state["evidence"]]
        limitations = list(state["limitations"])
        usage = {}
        if settings.llm_provider == "openai":
            try:
                budget.remaining()
                store.reserve(job, "model", settings)
                analysis, usage = OpenAIProvider(settings, connectors.http).analyze(
                    incident, evidence
                )
                budget.remaining()
                ids = {x.id for x in evidence}
                operational_ids = {
                    x.id
                    for x in evidence
                    if x.kind in {"metrics", "logs", "traces", "infrastructure"}
                }
                if any(
                    not set(h.supporting_evidence + h.contradicting_evidence) <= ids
                    or not set(h.supporting_evidence) & operational_ids
                    for h in analysis.hypotheses
                ):
                    raise ValueError("Unknown evidence citations")
            except (ConnectorError, ValueError, BudgetExceeded) as exc:
                analysis = insufficient(
                    "Model analysis could not be validated; engineer review required"
                )
                limitations.append("model: " + type(exc).__name__)
        else:
            analysis = deterministic_analysis(evidence)
            limitations.append("Deterministic signature baseline; no LLM reasoning was performed")
        if settings.decision_provider != "disabled":
            try:
                if settings.decision_provider == "jev":
                    store.reserve(job, "model", settings)
                adapter = (
                    JevDecisionAdapter(settings, connectors.http)
                    if settings.decision_provider == "jev"
                    else RuleDecisionAdapter()
                )
                decision = adapter.decide(
                    "evidence_sufficiency",
                    ["sufficient", "insufficient"],
                    {
                        "hypotheses": analysis.model_dump(mode="json")["hypotheses"],
                        "evidence": [x.model_dump(mode="json") for x in evidence],
                    },
                )
                usage["decision"] = decision
                if decision["choice"] == "insufficient":
                    analysis = insufficient(
                        "Decision layer flagged insufficient evidence; review required"
                    )
            except (ConnectorError, ValueError, BudgetExceeded) as exc:
                limitations.append("decision: " + type(exc).__name__ + "; baseline retained")
        result = {
            "analysis": redact(analysis.model_dump(mode="json")),
            "provider_usage": usage,
            "limitations": limitations,
            "provider": settings.llm_provider,
            "model": settings.openai_model if settings.llm_provider == "openai" else None,
        }
        store.checkpoint(job, "analyze", result)
        return result

    def report(state):
        budget.remaining()
        timeline = [
            {**event, "created_at": event["created_at"].isoformat()}
            for event in store.timeline(job["incident_id"])
        ]
        current = store.get_investigation(job["id"])
        result = Report(
            incident_id=job["incident_id"],
            investigation_id=str(job["id"]),
            mode=job["mode"],
            service=incident["service"],
            environment=incident["environment"],
            analysis=Analysis.model_validate(state["analysis"]),
            evidence=state["evidence"],
            timeline=timeline,
            limitations=state["limitations"]
            + [
                "Hypotheses require engineer review; no remediation executed",
                "Confidence labels are not calibrated probabilities",
            ],
            usage={
                **current["budget"],
                "provider_usage": state.get("provider_usage", {}),
                "cost_usd": None,
            },
            provider=state.get("provider", settings.llm_provider),
            model=state.get("model"),
        )
        # Assert all report renderings remain within the configured report budget.
        if len(json.dumps(result.model_dump(mode="json"))) > settings.max_response_bytes * 2:
            raise BudgetExceeded("Report size budget exceeded")
        return {"report": result.model_dump(mode="json")}

    builder = StateGraph(InvestigationState)

    def traced(name, operation):
        def wrapped(state):
            from opentelemetry import trace

            with trace.get_tracer("ai_sre_agent.workflow").start_as_current_span(name) as span:
                span.set_attribute("investigation.id", str(job["id"]))
                span.set_attribute("incident.id", job["incident_id"])
                return operation(state)

        return wrapped

    for name, node in [("collect", collect), ("analyze", analyze), ("report", report)]:
        builder.add_node(name, traced(name, node))
    builder.add_edge(START, "collect")
    builder.add_edge("collect", "analyze")
    builder.add_edge("analyze", "report")
    builder.add_edge("report", END)
    # Application-owned PostgreSQL checkpoints fence writes by lease, avoiding
    # a stale worker overwriting a newer attempt's graph state.
    output = builder.compile().invoke({}, config={"recursion_limit": 8})
    return Report.model_validate(output["report"])
