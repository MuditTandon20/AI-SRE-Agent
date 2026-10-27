# PRD implementation and verification status

This document distinguishes implemented code from release acceptance. Full production readiness and external-provider verification cannot be established from local fixture tests.

| Milestone | Implemented | Verification and remaining gates |
| --- | --- | --- |
| M0 foundation | Settings, package boundaries, complete dependencies, migrations, incident CRUD subset, readiness, CI | Local PostgreSQL tests and Python 3.12 image build; hosted CI still needs a pushed run |
| M1 incident lab | Separate sample service, failure/reset controls, traffic, Prometheus/Alertmanager, Loki/Alloy, Tempo | Docker stack ran; actual application-error alert/log/trace path verified; all five live scenario variants still need repeated release runs |
| M2 ingestion/evidence | Grouped alerts, stable occurrences, concurrent deduplication, transactional enqueue, query bounds and provenance | Integration tests plus automatic live-local alert-to-investigation path |
| M3 investigator | Single LangGraph graph, durable stage checkpoints, shared budgets, structured reports/citations, runbooks, abstention, OpenAI adapter | Deterministic fixture/live-local reports verified; OpenAI request/response contract mocked; actual LLM quality, access, latency and billing not verified |
| M4 evaluation | Thirty synthetic variants, baseline metrics, mock/adversarial/recovery tests, benchmark output | Synthetic suite runs; held-out real-world cases, independent causal-support scoring and manual MTTI baseline remain |
| M5 notifications | Timeline, transactional outbox, leases/retries, severity filters, fixture send, Slack MCP/SES adapters, authenticated delivery correlation and explicit retry | Mock contracts/fixture delivery/retry exhaustion tested; live destinations and provider-event relay setup remain |
| M6 traces | Demo OTLP instrumentation, Tempo search/full-trace retrieval, opt-in workflow spans | Actual exported demo traces observed; trace connector contract tests pass; live downstream-span diagnosis needs dedicated evaluation |
| M7 decisions | Provider-neutral decisions, rules baseline, Jev System One adapter, optional comparison runner | Official request shape verified and mocked; real Jev vs LLM/rules comparison and adoption decision remain |
| M8 memory | Explicit report archival, scoped keyword retrieval, history provenance and deletion | Retrieval/isolation/deletion tested; pgvector deferred pending evidence it improves keyword baseline |
| M9 changes/infrastructure | Read-only GitHub recent commits and namespace-scoped Kubernetes pod status | Mocked bounded contracts tested; real installation access and deployment-system correlation benchmarks remain; Jenkins/Argo CD are future adapters |
| M10 resilience | Concurrent workers, queue backpressure, lease fencing, attempts, checkpoints, cancellation, heartbeat, workflow metrics, load tool | Crash/recovery/call-budget tests; local read-load experiment; global provider rate limiting and larger deployment limits remain |
| M11 approved remediation | Independent local-demo executor, immutable proposals/digests, distinct approval role, expiry/revocation/preconditions/idempotency/postcheck | Non-production tests cover unauthorized/expired/tampered/replayed/changed-state actions; production actions intentionally absent |
| M12 deployment | Non-root image, core/demo profiles, dependency constraints, packaging CI, Kubernetes templates, operations guides | Compose image/runtime and YAML parsing verified; Kubernetes clean deployment, backup/restore exercise, security review and production acceptance remain |

## Local validation results

- **84 automated tests passed**, including PostgreSQL integration tests using disposable schemas; Ruff checks/formatting and installed dependency checks passed.
- The isolated `ai-sre-lab` Compose project built and ran on Docker Desktop with Linux/Python 3.12 application containers. API, worker and PostgreSQL health checks passed. The earlier root Compose database/services were preserved.
- Actual demo failure -> Prometheus alert -> Alertmanager webhook -> durable worker -> live-telemetry report was observed, with Loki evidence and exported Tempo traces. `artifacts/live-lab-report.json` records the sample. The smoke harness initially raced an application-container refresh; rerunning after the refresh passed.
- The final fixture-mode container smoke verified readiness, public documentation, authenticated incident creation, worker processing and a cited report. The lab was left running in fixture mode with the demo healthy.
- The 30-case synthetic benchmark completed. Its signature/abstention results are an internal contract check, not independently validated diagnostic accuracy.
- A local read-load experiment completed **100/100 requests** at concurrency four, with approximately **22 ms p50 / 38 ms p95**. This small single-machine run is not a production capacity estimate.
- Wheel and source archives built under `dist/`; package inspection verified migration/runbook resources, console entrypoints, source test/lab files and private-file exclusions. No registry publication occurred.
- No OpenAI or Jev key was configured. Live paid-provider inference, external Slack/SES delivery and Kubernetes deployment were not attempted or marked complete.

## Requirement coverage

FR-01–FR-11 have working local implementations and automated coverage. FR-12–FR-15 have optional adapters and mocked protocol coverage; real account/destination acceptance remains. FR-16 runs a synthetic benchmark with explicit limits. FR-17–FR-18 have initial keyword-history/change connectors, not all future providers. FR-19–FR-20 have a tested local-only approval/execution boundary.

## Acceptance boundaries

- Default analysis is a deterministic signature baseline, not an LLM. Fixture output is visibly labeled; live mode fetches real backend data even when the workload is the synthetic demo.
- Thirty synthetic fixtures verify known signatures and abstention behavior. Their success rate must not be advertised as real-world root-cause accuracy.
- Citation integrity does not prove causal support. Unknown prices and unmeasured cost are `null`, not zero.
- Slack MCP tool schemas differ. The implemented restricted schema needs validation against the chosen server. SES/MCP acceptance does not mean final delivery; exactly-once external sends are not promised.
- Static tokens identify roles, not individual operators. Production identity, per-service authorization and stronger database-role separation remain hardening work.
- Kubernetes files are reviewable templates, not proof of a deployed or secure production system. No cloud resources, external messages, repository releases or production actions were created during this run.
- External trial installations and public release review remain human/community gates.

Validation artifacts are written under ignored `artifacts/`. The repository's original README was preserved under `docs/archive/` before the current guide was rewritten.
