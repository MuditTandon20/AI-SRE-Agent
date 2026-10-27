"""Versioned public evidence/report schemas; no vendor-specific types."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    observed_at: datetime
    retrieved_at: datetime = Field(default_factory=utcnow)
    query: str
    resource: str
    content: str
    source_url: str | None = None
    kind: Literal["metrics", "logs", "traces", "runbook", "history", "change", "infrastructure"]
    truncated: bool = False
    synthetic: bool = False


class Hypothesis(StrictModel):
    cause: str = Field(min_length=1, max_length=500)
    reasoning: str = Field(min_length=1, max_length=3000)
    supporting_evidence: list[str] = Field(min_length=1, max_length=10)
    contradicting_evidence: list[str] = Field(max_length=10)
    confidence: Literal["low", "medium", "high"]


class Analysis(StrictModel):
    summary: str = Field(min_length=1, max_length=3000)
    outcome: Literal["hypotheses", "insufficient_evidence"]
    hypotheses: list[Hypothesis] = Field(max_length=5)
    missing_evidence: list[str] = Field(max_length=10)
    recommendations: list[str] = Field(max_length=10)

    @model_validator(mode="after")
    def check_outcome(self):
        if self.outcome == "hypotheses" and not self.hypotheses:
            raise ValueError("Hypothesis outcome requires supporting hypotheses")
        if self.outcome == "insufficient_evidence" and not self.missing_evidence:
            raise ValueError("Insufficient evidence must explain missing signals")
        return self


class Report(StrictModel):
    schema_version: str = "1"
    incident_id: int
    investigation_id: str
    mode: Literal["fixture", "live"]
    generated_at: datetime = Field(default_factory=utcnow)
    service: str
    environment: str
    analysis: Analysis
    evidence: list[Evidence]
    timeline: list[dict]
    limitations: list[str]
    usage: dict
    provider: str
    model: str | None = None
    prompt_version: str = "1"

    @model_validator(mode="after")
    def check_citations(self):
        ids = {item.id for item in self.evidence}
        if len(ids) != len(self.evidence):
            raise ValueError("Duplicate evidence IDs")
        for hypothesis in self.analysis.hypotheses:
            cited = hypothesis.supporting_evidence + hypothesis.contradicting_evidence
            if not set(cited) <= ids:
                raise ValueError("Hypothesis cites unknown evidence")
            if set(hypothesis.supporting_evidence) & set(hypothesis.contradicting_evidence):
                raise ValueError("Evidence cannot support and contradict the same hypothesis")
            if not any(
                item.id in hypothesis.supporting_evidence
                and item.kind in {"metrics", "logs", "traces", "infrastructure"}
                for item in self.evidence
            ):
                raise ValueError("Current causal claims require current operational evidence")
        if self.mode == "live" and any(item.synthetic for item in self.evidence):
            raise ValueError("Synthetic evidence cannot appear in a live report")
        return self


class InvestigationRequest(StrictModel):
    scenario: str | None = Field(default=None, max_length=100)


class Alert(StrictModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None
    generatorURL: str | None = None
    fingerprint: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_labels(self):
        if not self.labels.get("alertname"):
            raise ValueError("alertname label is required")
        if self.startsAt.tzinfo is None:
            raise ValueError("startsAt requires timezone")
        return self


class AlertWebhook(BaseModel):
    # Alertmanager adds envelope fields across versions.
    model_config = ConfigDict(extra="ignore")
    alerts: list[Alert] = Field(min_length=1, max_length=100)


def markdown_report(report: Report) -> str:
    lines = [
        f"# Incident {report.incident_id}",
        f"Mode: **{report.mode}** | Outcome: **{report.analysis.outcome}**",
        f"Service: {report.service} / {report.environment}",
        "",
        report.analysis.summary,
        "",
        "## Hypotheses",
    ]
    for rank, item in enumerate(report.analysis.hypotheses, 1):
        lines += [
            f"{rank}. {item.cause} ({item.confidence}; uncalibrated)",
            item.reasoning,
            "Evidence: " + ", ".join(item.supporting_evidence),
        ]
    lines += ["", "## Missing evidence"] + [f"- {x}" for x in report.analysis.missing_evidence]
    lines += ["", "## Suggested next steps (not executed)"]
    lines += [f"- {x}" for x in report.analysis.recommendations]
    lines += ["", "## Evidence"]
    for item in report.evidence:
        lines += [
            f"- {item.id}: {item.source} / {item.observed_at.isoformat()}",
            f"  Query: {item.query}",
            f"  {item.content}",
        ]
    lines += ["", "## Limitations"] + [f"- {x}" for x in report.limitations]
    return "\n".join(lines) + "\n"
