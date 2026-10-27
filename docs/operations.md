# Operations and deployment gates

## Recovery and scaling

Workers use database row locks, leases and attempt tokens. Start additional worker replicas only after load testing your PostgreSQL pool capacity and provider limits. Each process has a maximum pool size of five; configure replica counts accordingly. Queue backpressure is bounded by `QUEUE_LIMIT`; provider requests remain bounded by per-investigation budgets. Global provider rate limits across installations are not implemented.

Check `/investigations/{id}` for state/attempt/error and `/incidents/{id}/timeline` for sanitized events. A cancelled or superseded worker cannot commit checkpoints/reports. Expired jobs can be reclaimed up to `MAX_JOB_ATTEMPTS`. Stage recovery reuses persisted collection/analysis. If the original investigation deadline has passed, start a new investigation rather than resetting its budget silently.

`/metrics` includes HTTP metrics and database-derived investigation/call counts. Worker logs carry investigation IDs and error categories. `OTEL_ENABLED` opts into workflow spans; exporter environment configuration determines where traces go. External model/notification payloads exclude raw credentials and notification messages omit raw logs. Redaction is best-effort pattern filtering; operators must define what data is allowed to leave their environment.

## Database migration, backup and upgrade

Migrations are explicit, transactional, checksummed and advisory-locked. Stop accepting work or drain the queue for an upgrade that changes public contracts. Back up the database using your existing PostgreSQL tooling; record the application version and migration history with the backup. Restore first into a separate empty database, run integrity/readiness checks and inspect sample reports before changing application connection settings.

The lab database is an internal Compose service; an example dump command is `docker compose --env-file .env -f infra/compose.yml exec db pg_dump -U <database-user> -d <database-name> -Fc -f /tmp/ai-sre-backup.dump`. Copy the dump out with `docker compose ... cp db:/tmp/ai-sre-backup.dump <private-backup-path>`. Do not put credentials in shell history. Follow PostgreSQL's version compatibility requirements for restore tooling.

Rollback requires a compatible prior application/database version or restoring the pre-upgrade backup. There is no automatic destructive downgrade. Do not run a root-volume deletion to repair migration mismatches.

Retention previews by default. Applying it removes only old resolved incidents without remediation proposals; evidence, checkpoints, reports, outbox and incident-linked memory cascade. Audit records for open incidents and remediation proposals remain. Independent external logs, provider-retained data, tracing backends and backups need their own retention/deletion policies.

## Notification failure handling

`GET /notifications` exposes pending/accepted/delivered/failed attempts and sanitized error classes. Automatic retries stop after three attempts. Before using `POST /notifications/{id}/retry`, reconcile its provider ID and destination to avoid duplicates. A successful send is provider acceptance, not final delivery. Configure `SES_MIN_SEVERITY` and `SLACK_MIN_SEVERITY` to filter channel routing. A trusted provider-event relay may call `/webhooks/delivery/{id}` with a distinct `DELIVERY_TOKEN`, exact `provider_id` and `delivered`/`failed` state. Verify provider signatures upstream; raw SNS subscription/event handling is not supplied.

## Local remediation test

Only `reset_demo` for the local demo is implemented. Keep `REMEDIATION_ENABLED=false` for ordinary use. To test it, explicitly enable it for API and executor, configure distinct operator/approver/executor tokens and `EXECUTOR_URL=http://executor:8002`, and start the `remediation` and `demo` profiles. The executor also needs the demo-control token and its fixed `DEMO_CONTROL_URL`.

Create a proposal, inspect its immutable body/digest, approve with the separate approver credential, then request execution through the operator API. The executor independently validates approval and the exact expected scenario. Test fixtures cover no approval, wrong credentials, expiry, body tampering, revocation, changed preconditions and replay after completion.

If execution was interrupted and the state is `executing`, inspect the demo's actual state and reconcile manually. Do not overwrite a proposal or reset its state to force a retry. There are no production write tools, unrestricted shell commands or automatic infrastructure rollback actions.

## Kubernetes templates

`infra/kubernetes/` provides API/worker deployments, config, migration job, service, network policy and an example read-only connector Role. These are templates, not an already deployed or certified production installation.

Before applying: build/publish an image to your own registry, pin the reviewed digest, supply an external PostgreSQL database, replace backend hostnames, provide a private `ai-sre-secrets` Secret with database/API/webhook credentials, and configure database TLS roots. The secret is deliberately absent from version control. Use separate database roles for migrations and runtime as you harden the installation; the templates share a secret name for initial review and do not establish that separation automatically.

Apply namespace/config/secrets, run and wait for the migration job, then apply API/worker/service/network resources. A new migration release needs a uniquely named job or an explicitly managed replacement. Configure ingress authentication/TLS and endpoint-specific network destinations for your cluster; the sample network policy restricts ports but still needs destination review. Pods do not mount service-account tokens automatically. Resource values are starting limits, not measured production sizing.

Production readiness requires clean-cluster deployment, real backup/restore, upgrade/rollback, dependency/image security review, load limits, named operator identity/RBAC, real-provider evaluation, retention decisions and documented operational ownership. None of these gates is waived by passing local tests. Kafka, pgvector, OPA and additional vendors remain conditional on measured requirements rather than mandatory infrastructure.
