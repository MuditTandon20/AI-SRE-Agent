# Full-stack developer quickstart

The project now has a runnable self-hosted investigation stack and optional later-phase adapters. This is development software, not a production-readiness certification. See [implementation status](implementation-status.md) for exactly what was tested.

## Start the isolated demo

Prerequisites: Python 3.10+, Docker Desktop/Engine with Compose v2, and enough disk/network capacity to build the application and fetch six observability/database images. Initial image downloads are excluded from the onboarding time target. Host validation used Windows with Docker Desktop Linux containers; the application image uses Python 3.12.

From the repository root, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -c constraints.txt -r requirements-dev.txt
python -m ai_sre_agent.bootstrap
docker compose --env-file .env -f infra/compose.yml --profile demo up -d --build
python -m ai_sre_agent.cli investigate --scenario database_pool
```

On Linux/macOS activate with `source .venv/bin/activate`. Bootstrap preserves an existing `.env`; otherwise it creates local database credentials. It generates role/control tokens and Alertmanager configuration under ignored `.demo/`. Keep these files private. Existing root Compose services and data use different project names, volumes and API/Prometheus ports.

The default run is **fixture mode** with a deterministic signature baseline and no paid provider calls. Reports are explicitly labeled. The fixture command does not inject a real failure. To control the actual synthetic sample service:

```powershell
python -m ai_sre_agent.cli scenario --scenario application_error
python -m ai_sre_agent.lab traffic --seconds 60
python -m ai_sre_agent.cli reset
```

Controls require the generated demo token; `cli` loads it without printing it. The sample app writes JSON logs to a dedicated named volume, Alloy forwards them to Loki, and OTLP traces go to Tempo. Database-pool failure uses a real small PostgreSQL pool in Compose; without database configuration in a native run it is explicitly simulated. Other sample scenarios are application error, downstream timeout, configuration error and ambiguous latency.

## Use live local telemetry

Live means querying configured backends rather than replaying fixture evidence. It does not imply production data or live LLM use.

```powershell
$env:EXECUTION_MODE = 'live'
docker compose --env-file .env -f infra/compose.yml --profile demo up -d api worker
python -m ai_sre_agent.cli scenario --scenario application_error
python -m ai_sre_agent.lab traffic --seconds 60
python -m ai_sre_agent.cli investigate --live
python -m ai_sre_agent.cli reset
```

Prometheus alerts also trigger automatic investigations through Alertmanager. The error-rate alert needs enough traffic and approximately a minute for scrape/rate/alert/group timing. The CLI's manual investigation is separate from the automatically created incident. `python scripts/smoke_lab.py` injects an application-error scenario, checks the alert/report and traces, and resets in a `finally` block. Run it against the isolated lab only.

To restore fixture mode, set `$env:EXECUTION_MODE = 'fixture'` and recreate API/worker with the same Compose command. Ensure their modes match; queued investigations retain their original mode. Complete or cancel pending jobs before switching.

## Local endpoints

| Service | Address |
| --- | --- |
| API health/readiness | `http://localhost:8010/health`, `/ready` |
| API documentation | `http://localhost:8010/docs` (use Authorize for protected requests) |
| API metrics | `http://localhost:8010/metrics` |
| Demo | `http://localhost:8001/checkout` |
| Prometheus | `http://localhost:9091` |
| Alertmanager | `http://localhost:9094` |
| Loki | `http://localhost:3100` |
| Tempo | `http://localhost:3200` |

Incident/report/history/control endpoints require bearer authentication when tokens are configured. Health/readiness/metrics remain accessible for local monitoring. Root fixtures may run without tokens; live mode fails closed for unconfigured API/webhook roles. Restrict monitoring endpoints at your network boundary.

## Connect an existing stack

Set `EXECUTION_MODE=live`, distinct API/webhook tokens, database settings and `PROMETHEUS_URL`/`LOKI_URL` in `.env`. Configure service/environment labels in your incoming alerts and log streams. The current metrics query expects `demo_requests_total{service,code}`; adapt the reviewed query template for your instrumentation before expecting useful metrics. Loki retrieval expects `service` and `environment` stream labels.

Run `docker compose --env-file .env -f infra/compose.yml up -d --build` without the demo profile to start just PostgreSQL, migration, API and worker. Explicit endpoint values from `.env` override the internal demo-backend defaults. Native operation reads endpoint settings directly from `.env`. Do not assume host `localhost` inside a container points at your host.

For native development, use [the database/API foundation guide](getting-started.md), then run `python -m ai_sre_agent.worker` in a second terminal. Apply migrations explicitly before starting the worker. The root `docker-compose.yml` remains the earlier native-development database/Prometheus setup.

## Optional integrations

- OpenAI: set `LLM_PROVIDER=openai`, `OPENAI_API_KEY` and `OPENAI_MODEL=gpt-6.1-sol` (or another explicitly compatible model). Documentation confirms the identifier; account access, quota, pricing and live quality remain installation checks. Otherwise use `deterministic`.
- Jev: set `DECISION_PROVIDER=jev`, `TYPESAFE_API_KEY` and optionally `JEV_MODEL`. `rules` enables a local sufficiency baseline. Neither mode authorizes actions.
- Tempo: set `TEMPO_URL` for trace retrieval. The bundled demo exports traces automatically. Enable agent workflow export with `OTEL_ENABLED=true` and standard OTLP exporter environment configuration.
- GitHub: set `GITHUB_REPOSITORY=owner/repository` and optionally a read-only `GITHUB_TOKEN`.
- Kubernetes: set `KUBERNETES_URL`, `KUBERNETES_NAMESPACE`, a read-only `KUBERNETES_TOKEN` and, where needed, `KUBERNETES_CA_FILE`. Never disable TLS verification to bypass a cluster certificate error.
- Notifications: `NOTIFICATION_MODE=fixture` exercises the outbox locally. `live` requires explicit destinations. Slack uses `SLACK_MCP_URL`, optional MCP token, `SLACK_CHANNEL` and the configured tool schema. SES uses `SES_REGION`, `SES_SENDER`, `SES_RECIPIENT` and the AWS credential chain. Neither is enabled by default.
- Memory: explicitly archive a completed report with `POST /investigations/{id}/archive`; retrieve through `/history` or subsequent scoped investigations. Historical hypotheses remain unverified context. Delete indexed reports through `/history/{id}`.

Environment changes inside `.env` require container recreation. Bootstrap-generated role tokens override same-named values through the lab env file. Do not put paid-provider keys in issue reports or command-line examples.

## Testing, evaluation and operations

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
python -m ai_sre_agent.evaluate --output artifacts/benchmark.json
```

Set `TEST_DATABASE_URL` privately to enable isolated-schema PostgreSQL tests. Without it, database tests skip. The evaluation command runs 30 synthetic variants; it does not establish general diagnostic accuracy or an MTTI improvement. `--live-model` and `--jev` make explicit paid-provider comparisons and require credentials supplied in the process environment.

`python scripts/load_test.py` exercises bounded read requests; supply `API_TOKEN` privately. It is an experiment, not a throughput guarantee. `python -m ai_sre_agent.maintenance` previews retention; `--apply` deletes eligible expired resolved incidents and cascaded investigation state. Backups retain data until separately rotated.

Stop this stack with `docker compose --env-file .env -f infra/compose.yml --profile demo down`. Volumes persist. No command in the quickstart deletes volumes. See [operations](operations.md) before upgrades, recovery or Kubernetes deployment.
