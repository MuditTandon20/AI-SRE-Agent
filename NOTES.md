> Historical snapshot: this guide describes the repository before M0. For current dependencies, versioned migrations, API retrieval and readiness checks, use [the development quickstart](docs/getting-started.md).

# AI SRE Agent: Repository Guide

This repository contains a FastAPI service for recording incidents in PostgreSQL and exposing HTTP telemetry to Prometheus. It is an early foundation for an SRE agent: AI diagnosis, telemetry correlation, runbook matching, and remediation approval are not implemented yet.

## Current architecture

```text
HTTP client -> FastAPI (host port 8000) -> PostgreSQL (host port 5433)
                   |
                /metrics <- Prometheus (host port 9090)
```

The API runs locally in Python. Docker Compose runs PostgreSQL 16 and Prometheus. Prometheus scrapes `host.docker.internal:8000/metrics` every five seconds. This hostname is intended for Docker Desktop; other Docker environments may require host networking configuration.

## Repository files

| File | Purpose |
| --- | --- |
| `main.py` | FastAPI application, database pool, request logging, metrics, and incident creation. |
| `docker-compose.yml` | PostgreSQL and Prometheus services, with a persistent `pgdata` database volume. |
| `prometheus.yml` | Scrape configuration for the API. |
| `db_check.py` | Connects to PostgreSQL and prints incident IDs, titles, and severities. |
| `requirements.txt` | Currently lists only FastAPI and Uvicorn; additional imports require installation below. |
| `README.md` | Existing project overview, including planned capabilities; some setup and API details are outdated. |
| `.env` | Local database configuration; keep credentials private. |
| `LICENSE` | MIT license. |

## Local setup

Use Python 3.10 or newer and Docker with Compose. Run commands from the repository root.

### 1. Install Python dependencies

In Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install python-dotenv prometheus-client "psycopg[binary,pool]"
```

The second installation command supplies packages imported by the code but currently missing from `requirements.txt`.

### 2. Configure the database

Create or update a local `.env` with your own values:

```dotenv
POSTGRES_USER=sre
POSTGRES_PASSWORD=replace_with_a_local_password
POSTGRES_DB=sre_agent
```

Both Python scripts use `load_dotenv()`. Compose uses these variables to initialize PostgreSQL. The Python database host and port are hardcoded to `localhost:5433`.

```powershell
docker compose up -d
docker compose exec db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Wait until PostgreSQL accepts connections before proceeding. Initialization credentials apply when the database volume is first created; changing `.env` does not update credentials inside an existing database volume.

### 3. Initialize the incidents table

The repository does not include schema migrations or create the table automatically. Open a database session:

```powershell
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Run this proposed minimal schema, which satisfies the current insert and query operations:

```sql
CREATE TABLE IF NOT EXISTS incidents (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    symptom TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('SEV1', 'SEV2', 'SEV3')),
    status TEXT NOT NULL DEFAULT 'open',
    detected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Exit with `\q`. This is a suggested local bootstrap schema, not a migration supplied by the application. If a table already exists, inspect its columns and defaults; `IF NOT EXISTS` does not modify an existing table.

### 4. Run the API

```powershell
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- API: <http://localhost:8000>
- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- Metrics: <http://localhost:8000/metrics>
- Prometheus: <http://localhost:9090>

## API behavior

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/` | Returns `{"message":"AI SRE Agent API is running"}`. |
| GET | `/health` | Returns `{"status":"ok"}`; does not check the database. |
| GET | `/slow` | Waits approximately five seconds and returns `{"status":"finally done"}`. |
| GET | `/crash` | Raises an exception, producing HTTP 500; does not intentionally terminate the server. |
| GET | `/metrics` | Exposes Prometheus metrics. |
| POST | `/incidents` | Inserts an incident and returns HTTP 201 with `id`, `status`, and `detected_at`. |

Incident creation requires four fields: `title`, `path`, `symptom`, and `severity`. Severity must be `SEV1`, `SEV2`, or `SEV3`. There is no custom nonempty-string validation for the other fields. Invalid request data returns FastAPI validation errors, normally HTTP 422.

Example in PowerShell:

```powershell
$incidentBody = @{
    title = 'Checkout failures'
    path = '/checkout'
    symptom = 'Elevated HTTP 500 responses'
    severity = 'SEV1'
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8000/incidents -ContentType 'application/json' -Body $incidentBody
python db_check.py
```

The response ID and timestamp come from PostgreSQL. The status comes from the table default; with the suggested schema, it is `open`. The insert uses parameterized SQL and commits on successful connection-context exit.

## Observability

Middleware records the HTTP method, request path, response status, and rounded latency in milliseconds. Its `finally` block also records failures with a default status of 500.

Application metrics include:

- `http_requests_total`, labeled by `method`, `route`, and `status`.
- `http_request_duration_seconds`, a histogram labeled by `method` and `route`, with configured finite buckets from 0.005 to 10 seconds.

Metrics use route templates when available and `unmatched` otherwise. The Prometheus client also exposes its default collectors. Useful PromQL expressions are:

```promql
up{job="sre-app"}
rate(http_requests_total{job="sre-app"}[5m])
rate(http_requests_total{job="sre-app",status="500"}[5m])
histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="sre-app"}[5m])))
```

To produce sample traffic:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/slow
curl.exe -i http://localhost:8000/crash
```

Expect an error response from `/crash`. Check the Prometheus target page at <http://localhost:9090/targets> to confirm scraping works.

## Implementation limits and next steps

- Add all runtime imports to `requirements.txt` and select reproducible dependency versions.
- Add versioned schema migrations; there is currently no automated database initialization.
- Add database readiness checks. The pool opens during application startup without explicitly waiting for a connection, and `/health` only reports API liveness.
- Add incident retrieval and update endpoints; only creation is currently implemented.
- Add authentication and authorization before exposing incident writes or simulation routes outside a local environment.
- Add tests for validation, persistence, and telemetry. No automated test files are present in the repository.
- Implement alert integrations, AI analysis, runbooks, and approval workflows as future features.

## Troubleshooting and shutdown

| Symptom | Check |
| --- | --- |
| Missing Python module | Install both the requirements file and the additional packages listed above in the active environment. |
| `KeyError` for a `POSTGRES_*` variable | Supply all three database variables in `.env` or the process environment. |
| Database connection timeout | Check `docker compose ps`, database readiness, credentials, and the host port mapping `5433:5432`. |
| `relation "incidents" does not exist` | Initialize the table in the database named by `POSTGRES_DB`. |
| Prometheus target is down | Ensure Uvicorn listens on `0.0.0.0:8000` and the container can resolve and reach `host.docker.internal`. |

Stop Uvicorn with Ctrl+C. Stop the supporting containers with `docker compose down`; the named PostgreSQL volume persists.

This guide was checked against the repository source and configuration. Running services and database behavior were not validated during this documentation review.
