"""Transactional application state, PostgreSQL queue, checkpoints and audit trail."""

from datetime import timedelta
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_sre_agent.domain import utcnow
from ai_sre_agent.safety import digest, redact


class QueueFull(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class Store:
    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def event(cur, incident_id, kind, details=None, investigation_id=None):
        cur.execute(
            "INSERT INTO timeline_events (incident_id, investigation_id, kind, details) VALUES (%s,%s,%s,%s)",
            (incident_id, investigation_id, kind, Jsonb(redact(details or {}))),
        )

    def _enqueue(self, cur, incident_id, settings, scenario=None):
        cur.execute("SELECT pg_advisory_xact_lock(7824601002)")
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (incident_id,))
        existing = cur.execute(
            "SELECT * FROM investigations WHERE incident_id=%s AND status IN ('queued','running')",
            (incident_id,),
        ).fetchone()
        if existing:
            return existing
        if not cur.execute("SELECT id FROM incidents WHERE id=%s", (incident_id,)).fetchone():
            raise KeyError("Incident not found")
        if (
            cur.execute(
                "SELECT count(*) AS n FROM investigations WHERE status IN ('queued','running')"
            ).fetchone()["n"]
            >= settings.queue_limit
        ):
            raise QueueFull("Investigation queue is full")
        row = cur.execute(
            "INSERT INTO investigations (id,incident_id,mode,scenario) VALUES (%s,%s,%s,%s) RETURNING *",
            (uuid4(), incident_id, settings.execution_mode, scenario),
        ).fetchone()
        self.event(cur, incident_id, "investigation.queued", {"mode": row["mode"]}, row["id"])
        return row

    def enqueue(self, incident_id, settings, scenario=None):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return self._enqueue(cur, incident_id, settings, scenario)

    def ingest_alerts(self, alerts, settings):
        result = []
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7824601002)")
            for alert in alerts:
                service = alert.labels.get("service", "demo-service")
                environment = alert.labels.get("environment", "local")
                occurrence = digest(
                    {
                        "source": "alertmanager",
                        "service": service,
                        "environment": environment,
                        "fingerprint": alert.fingerprint,
                        "start": alert.startsAt.isoformat(),
                    }
                )
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (occurrence,))
                previous = cur.execute(
                    "SELECT * FROM alert_occurrences WHERE occurrence_key=%s", (occurrence,)
                ).fetchone()
                if previous:
                    cur.execute(
                        "UPDATE alert_occurrences SET last_seen=now(), state=CASE WHEN %s='resolved' THEN 'resolved' ELSE state END WHERE occurrence_key=%s",
                        (alert.status, occurrence),
                    )
                    self.event(
                        cur,
                        previous["incident_id"],
                        "alert." + alert.status,
                        {"fingerprint": alert.fingerprint, "duplicate": True},
                    )
                    result.append({"incident_id": previous["incident_id"], "duplicate": True})
                    continue
                if alert.status == "resolved":
                    result.append({"ignored": "unknown resolved occurrence"})
                    continue
                severity = {"critical": "SEV1", "warning": "SEV2", "info": "SEV3"}.get(
                    alert.labels.get("severity", ""), alert.labels.get("severity", "SEV3")
                )
                if severity not in {"SEV1", "SEV2", "SEV3"}:
                    severity = "SEV3"
                row = cur.execute(
                    "INSERT INTO incidents (title,path,symptom,severity,service,environment) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (
                        redact(alert.annotations.get("summary", alert.labels["alertname"]))[:300],
                        alert.labels.get("path", "/")[:2048],
                        redact(alert.annotations.get("description", alert.labels["alertname"]))[
                            :10000
                        ],
                        severity,
                        service,
                        environment,
                    ),
                ).fetchone()
                incident_id = row["id"]
                cur.execute(
                    "INSERT INTO alert_occurrences (occurrence_key,incident_id,fingerprint,starts_at,state) VALUES (%s,%s,%s,%s,'firing')",
                    (occurrence, incident_id, alert.fingerprint, alert.startsAt),
                )
                self.event(cur, incident_id, "alert.firing", {"fingerprint": alert.fingerprint})
                investigation = self._enqueue(
                    cur,
                    incident_id,
                    settings,
                    alert.labels.get("scenario") if settings.execution_mode == "fixture" else None,
                )
                result.append(
                    {
                        "incident_id": incident_id,
                        "investigation_id": investigation["id"],
                        "duplicate": False,
                    }
                )
        return result

    def claim(self, settings):
        token = uuid4()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE investigations SET status='failed',error='attempts_exhausted',finished_at=now() "
                "WHERE status='running' AND lease_until<now() AND attempt >= %s",
                (settings.max_job_attempts,),
            )
            row = cur.execute(
                "WITH candidate AS (SELECT id FROM investigations WHERE "
                "((status='queued' AND next_attempt<=now()) OR (status='running' AND lease_until<now())) "
                "AND attempt<%s ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE investigations i SET status='running',attempt=i.attempt+1,lease_token=%s,"
                "lease_until=now()+(%s * interval '1 second'),started_at=COALESCE(i.started_at,now()) "
                "FROM candidate c WHERE i.id=c.id RETURNING i.*",
                (settings.max_job_attempts, token, settings.investigation_seconds + 30),
            ).fetchone()
            if row:
                self.event(
                    cur,
                    row["incident_id"],
                    "investigation.started",
                    {"attempt": row["attempt"]},
                    row["id"],
                )
            return row

    @staticmethod
    def fence(cur, job):
        row = cur.execute(
            "SELECT * FROM investigations WHERE id=%s AND status='running' AND lease_token=%s AND lease_until>now() FOR UPDATE",
            (job["id"], job["lease_token"]),
        ).fetchone()
        if not row:
            raise LeaseLost("Investigation lease expired or was cancelled")
        return row

    def get_investigation(self, investigation_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT * FROM investigations WHERE id=%s", (investigation_id,)
            ).fetchone()
            if row:
                row.pop("lease_token", None)
            return row

    def load_checkpoint(self, job, stage):
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT body FROM workflow_checkpoints WHERE investigation_id=%s AND stage=%s",
                (job["id"], stage),
            ).fetchone()
            return row[0] if row else None

    def reserve(self, job, kind, settings):
        """Reserve before side effects so a crash cannot reset call budgets."""
        key = "tool_calls" if kind == "tool" else "model_calls"
        limit = settings.max_tool_calls if kind == "tool" else settings.max_model_calls
        from ai_sre_agent.safety import BudgetExceeded

        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current = self.fence(cur, job)
            if (utcnow() - current["started_at"]).total_seconds() > settings.investigation_seconds:
                raise BudgetExceeded("Investigation deadline exceeded")
            usage = current["budget"]
            if usage.get(key, 0) >= limit:
                raise BudgetExceeded(f"{kind} budget exhausted")
            usage[key] = usage.get(key, 0) + 1
            cur.execute(
                "UPDATE investigations SET budget=%s WHERE id=%s", (Jsonb(usage), job["id"])
            )
            self.event(
                cur,
                job["incident_id"],
                "call.reserved",
                {"kind": kind, "count": usage[key]},
                job["id"],
            )
            return usage

    def checkpoint(self, job, stage, body):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.fence(cur, job)
            cur.execute(
                "INSERT INTO workflow_checkpoints (investigation_id,stage,body) VALUES (%s,%s,%s) "
                "ON CONFLICT (investigation_id,stage) DO UPDATE SET body=EXCLUDED.body,updated_at=now()",
                (job["id"], stage, Jsonb(redact(body))),
            )
            self.event(
                cur, job["incident_id"], "stage." + stage, {"status": "completed"}, job["id"]
            )

    def finish(self, job, report, settings):
        body = redact(report.model_dump(mode="json"))
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.fence(cur, job)
            for item in report.evidence:
                cur.execute(
                    "INSERT INTO evidence (id,investigation_id,body) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (item.id, job["id"], Jsonb(redact(item.model_dump(mode="json")))),
                )
            cur.execute(
                "INSERT INTO reports (investigation_id,incident_id,body) VALUES (%s,%s,%s)",
                (job["id"], job["incident_id"], Jsonb(body)),
            )
            cur.execute(
                "UPDATE investigations SET status='completed',finished_at=now(),lease_token=NULL,lease_until=NULL WHERE id=%s",
                (job["id"],),
            )
            self.event(
                cur,
                job["incident_id"],
                "report.completed",
                {"outcome": report.analysis.outcome},
                job["id"],
            )
            if settings.notification_mode != "disabled":
                severity = cur.execute(
                    "SELECT severity FROM incidents WHERE id=%s", (job["incident_id"],)
                ).fetchone()["severity"]
                rank = {"SEV1": 1, "SEV2": 2, "SEV3": 3}
                destinations = [("slack", settings.slack_channel), ("ses", settings.ses_recipient)]
                if settings.notification_mode == "fixture" and not any(x[1] for x in destinations):
                    destinations = [("fixture", "local-demo")]
                for channel, destination in destinations:
                    minimum = (
                        settings.ses_min_severity
                        if channel == "ses"
                        else settings.slack_min_severity
                    )
                    if rank.get(severity, 3) > rank[minimum]:
                        continue
                    if destination:
                        cur.execute(
                            "INSERT INTO notification_outbox (id,investigation_id,channel,destination,mode) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (uuid4(), job["id"], channel, destination, settings.notification_mode),
                        )

    def fail(self, job, error, settings, retryable=False):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current = self.fence(cur, job)
            retry = retryable and current["attempt"] < settings.max_job_attempts
            cur.execute(
                "UPDATE investigations SET status=%s,error=%s,lease_token=NULL,lease_until=NULL,"
                "next_attempt=now()+interval '5 seconds',finished_at=CASE WHEN %s THEN NULL ELSE now() END WHERE id=%s",
                ("queued" if retry else "failed", error[:200], retry, job["id"]),
            )
            self.event(
                cur,
                job["incident_id"],
                "investigation.retry" if retry else "investigation.failed",
                {"error": error[:200]},
                job["id"],
            )

    def report(self, investigation_id):
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT body FROM reports WHERE investigation_id=%s", (investigation_id,)
            ).fetchone()
            return row[0] if row else None

    def timeline(self, incident_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id,kind,details,created_at FROM timeline_events WHERE incident_id=%s ORDER BY id LIMIT 500",
                (incident_id,),
            ).fetchall()

    def cancel(self, investigation_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "UPDATE investigations SET status='cancelled',finished_at=now(),lease_token=NULL,lease_until=NULL WHERE id=%s AND status IN ('queued','running') RETURNING *",
                (investigation_id,),
            ).fetchone()
            if row:
                self.event(
                    cur,
                    row["incident_id"],
                    "investigation.cancelled",
                    investigation_id=investigation_id,
                )
            return row is not None

    def history(self, service, environment, query):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id,title,content,source,created_at FROM historical_documents WHERE service=%s AND environment=%s "
                "AND to_tsvector('english',title || ' ' || content) @@ plainto_tsquery('english',%s) "
                "ORDER BY created_at DESC LIMIT 3",
                (service, environment, query),
            ).fetchall()

    def archive_report(self, investigation_id):
        report = self.report(investigation_id)
        if not report:
            raise KeyError("Report not found")
        document_id = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO historical_documents (id,incident_id,service,environment,title,content,source) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    document_id,
                    report["incident_id"],
                    report["service"],
                    report["environment"],
                    report["analysis"]["summary"],
                    str(redact(report["analysis"]))[:10000],
                    "investigation:" + str(investigation_id),
                ),
            )
        return document_id

    def retention(self, days):
        cutoff = utcnow() - timedelta(days=days)
        with self.pool.connection() as conn:
            # Do not remove open incidents or records used by remediation proposals.
            rows = conn.execute(
                "DELETE FROM incidents i WHERE status='resolved' AND detected_at<%s "
                "AND NOT EXISTS (SELECT 1 FROM remediation_proposals p WHERE p.incident_id=i.id) RETURNING id",
                (cutoff,),
            ).fetchall()
            return len(rows)
