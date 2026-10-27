# Development quickstart

This guide covers native API/database development. The complete worker, observability lab, webhook ingestion and reporting workflow are documented in [the full-stack quickstart](full-stack-quickstart.md). See [implementation status](implementation-status.md) for verification and remaining release gates.

## Prerequisites and configuration

Use Python 3.10+ and Docker Compose. Local validation uses Python 3.10 and PostgreSQL 16; CI is configured for Python 3.10/3.12. Run these PowerShell commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -c constraints.txt -r requirements-dev.txt
```

For runtime only, install `requirements.txt`. On Linux/macOS activate with `source .venv/bin/activate`.

For a new installation, copy `.env.example` to `.env`, then change the password. Preserve an existing `.env`. Set `POSTGRES_HOST` and `POSTGRES_PORT` for your database; defaults are `localhost:5433`. Process environment variables override `.env`. Imports do not load credentials or open connections; startup validates configuration and waits for a bounded database connection attempt.

```powershell
docker compose up -d db prometheus
docker compose exec db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
python -m ai_sre_agent.migrate
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Use `--host 0.0.0.0` when the Prometheus container needs to reach the API through `host.docker.internal`; account for local firewall and network access. Authentication is not implemented yet, and simulation routes remain available, so restrict this development service to trusted local access.

## Safe migrations

Migrations are explicit, never run automatically on API startup. They use a transaction, advisory lock and recorded checksums. The initial migration creates a fresh incidents table or adopts a compatible existing table. Existing rows and nullable extra columns such as `resolved_at` are retained. Incompatible columns, missing required defaults, or extra required fields without defaults fail with a schema error; no destructive repair is attempted.

Back up existing databases before applying migrations. Checksum/version mismatches require operator review; do not edit an already applied migration. The initial compatibility check validates structure, not every legacy row or custom constraint. A preexisting custom constraint may still reject an otherwise valid insert. There is no automated downgrade command.

## API examples

Open <http://localhost:8000/docs> or use PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
$incidentBody = @{
    title = 'Checkout timeout'
    path = '/checkout'
    symptom = 'Database acquisition timeout'
    severity = 'SEV1'
} | ConvertTo-Json
$incident = Invoke-RestMethod -Method Post -Uri http://localhost:8000/incidents -ContentType 'application/json' -Body $incidentBody
Invoke-RestMethod "http://localhost:8000/incidents/$($incident.id)"
Invoke-RestMethod 'http://localhost:8000/incidents?limit=20&offset=0'
```

Creation retains HTTP 201 and `{id, status, detected_at}`. Retrieval includes the submitted fields. Lists return `{items, limit, offset}` ordered by newest ID first; `limit` is 1–100 and `offset` is 0–1,000,000. Missing IDs return 404. Empty/oversize strings, invalid severities and invalid pagination return 422. Offset pagination can shift while new records are created; cursor pagination is future work.

`/health` is liveness only. `/ready` verifies database access, recorded migration versions/checksums and the required table structure, returning 503 on failure. Database failures are returned without connection details. `/metrics` exposes per-application HTTP counters and latency histograms with bounded route labels. `/slow` still delays five seconds; `/crash` still intentionally returns 500. `python db_check.py` prints at most 100 recent incident summaries.

## Tests and linting

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Database tests skip unless `TEST_DATABASE_URL` points to a disposable PostgreSQL database owned by a test user allowed to create schemas. Tests create randomly named `test_sre_*` schemas and remove only those schemas in cleanup; they do not migrate `public`. Never use production credentials. Supply the connection URL privately in your process environment. CI provides an isolated PostgreSQL service and runs the database tests.

Integration coverage includes create/get/list, transaction rollback, migration idempotency, preserving existing data, incompatible-schema rejection, checksum tampering and readiness under schema drift. Unit/API tests run without a database or paid provider credentials.

Stop Uvicorn with Ctrl+C; `docker compose down` stops supporting services and retains the named data volume. Changing `.env` credentials does not change users/passwords already initialized inside that volume.
