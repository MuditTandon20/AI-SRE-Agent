# Architecture and integration contracts

The API accepts incidents and grouped Alertmanager events. PostgreSQL persists accepted work transactionally. Separate workers claim investigations with `FOR UPDATE SKIP LOCKED`, bounded leases and attempt tokens. Every checkpoint/report write checks the current token, status and lease while holding the investigation row lock. Cancellation invalidates the token. Calls are reserved in persisted budgets before requests, so a worker crash cannot reset call counts.

One LangGraph graph performs collection, analysis and reporting. Application-owned PostgreSQL stage checkpoints let retries reuse completed collection/analysis and enforce the same lease boundary as report persistence. This is stage recovery, not arbitrary mid-request resumption. Calls interrupted before their checkpoint can be repeated within the persisted budget. A process crash never guarantees exactly-once provider billing.

The graph uses fixed reviewed read-only operations. Models receive redacted evidence and return structured hypotheses; they receive no shell, executor tool, arbitrary endpoint selection or write credentials. A report has a schema version, actual evidence IDs, source/query/resource/timestamps, contradictions, uncertainty, missing evidence, recommendations and mode. Citation validation proves a reference exists, not that a causal claim is true.

## Connector contract

`Connectors.capabilities` declares version, permission scope and operations. Add a typed method that returns `list[Evidence]`, a fixed configured destination, explicit query limits, and fixtures exercising success/failure/redaction. Use `BoundedHTTP` for bounded reads with redirects disabled and explicit TLS verification. Provider credentials must not be stored in evidence, exceptions or report content. Private observability endpoints are allowed only through operator configuration.

Current operations:

| Connector | Scope and limits |
| --- | --- |
| Prometheus | One service metric range query, bounded window and step; currently the demo request-counter template |
| Loki | Service/environment selector, at most 100 log entries |
| Tempo | TraceQL service search, at most five search results and two full traces |
| GitHub | Configured repository, at most five recent commits; correlation only |
| Kubernetes | Configured namespace, at most 20 pods selected by `app=<service>`; get/list only |
| Runbooks | Configured directory or bundled resources, bounded Markdown files, top-three keyword matches |
| History | Explicitly archived reports, keyword retrieval within service/environment, at most three matches; unverified context |

Queries are intentionally narrow. An existing stack must expose compatible metric names/labels or adapt the Prometheus query method. General arbitrary PromQL/LogQL generation, deployment-system connectors and additional vendors are extension work, not automatically supported.

## Provider contracts

`OpenAIProvider.analyze` uses Responses with a strict JSON schema and disabled response storage. The exact configured model is retained. Refusal, incomplete output, invalid schema and invalid citations become explicit analysis limitations/insufficient-evidence results. Input character limits, output token limits, persisted call counts and deadlines bound work. Dollar cost is unknown unless separately measured with verified prices; `null` never means free.

`DecisionAdapter.decide` is provider-neutral. The optional Jev adapter uses TypeSafe's System One Choice API for evidence sufficiency. Rules, model-only and model-plus-decision comparisons can be run through the evaluation command. A Jev decision cannot authorize an action or turn missing evidence into proof. Probabilities are retained as provider outputs and are not claimed to be calibrated for this incident dataset.

## Notifications

Report persistence and notification enqueue happen in one transaction. An outbox worker leases items, retries bounded failures and exposes accepted/failed state. Live delivery requires explicit mode and destination configuration. The current Slack MCP contract expects a tool taking `channel` and `text`; configure `SLACK_TOOL_NAME` for that schema or adapt `SlackMCP` for your server. JSON and bounded SSE responses are supported for Streamable HTTP. SES uses the standard AWS credential chain and records `MessageId` as acceptance.

External notification delivery is at-least-once in crash/timeout windows; Slack MCP and SES do not provide a shared exactly-once transaction with PostgreSQL. Failed attempts stop after three sends. `SES_MIN_SEVERITY` and `SLACK_MIN_SEVERITY` select which incidents reach each destination. An independent authenticated delivery relay can update an accepted notification through `/webhooks/delivery/{id}` using its exact provider ID; duplicate terminal events are idempotent. Verifying AWS SNS signatures and transforming provider-specific callbacks is the relay's responsibility. Operator-triggered `/notifications/{id}/retry` requeues only failed items after reconciliation. `delivered` is never inferred from acceptance.

## Approval/execution boundary

Remediation is disabled by default. A separate executor supports only `reset_demo` for `demo-service/local`. A proposal is immutable and digested, expires, and includes an exact expected scenario. An independent approver token approves that digest. The executor reads persisted approval, checks expiry/digest/action/target/precondition, claims execution, resets the configured local lab, and verifies healthy state. Completed requests return the recorded result. An ambiguous `executing` state after a crash requires manual reconciliation; it is never blindly retried. Resetting a failed demo to healthy is the only supported effect; arbitrary shell, rollback of production changes, and production actions are absent.

Static tokens represent installation roles, not named user identity or enterprise RBAC. Production approval identity and stronger database-role separation require further design and verification.

## Upstream references

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Alertmanager webhook configuration](https://prometheus.io/docs/alerting/latest/configuration/)
- [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [GPT-6.1 Sol model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [TypeSafe System One API](https://docs.typesafe.ai/api/system-one) and [Choice schema](https://docs.typesafe.ai/primitives/choice)
- [MCP Streamable HTTP specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-03-26/basic/transports.mdx)
- [SES send-email API](https://docs.aws.amazon.com/botocore/latest/reference/services/sesv2/client/send_email.html)
