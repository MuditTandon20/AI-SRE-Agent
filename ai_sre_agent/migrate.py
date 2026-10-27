"""Explicit, transactional SQL migrations with compatible-table adoption."""

import hashlib
import sys
from importlib.resources import files

import psycopg
from psycopg.rows import dict_row

from ai_sre_agent.config import ConfigurationError, Settings


class SchemaError(RuntimeError):
    """Schema differs from the application contract; operator review is required."""


def migration_files() -> list[tuple[str, str, str]]:
    directory = files("ai_sre_agent").joinpath("migrations")
    result = []
    for path in sorted(directory.iterdir(), key=lambda path: path.name):
        if path.name.endswith(".sql"):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.replace("\r\n", "\n").encode()).hexdigest()
            result.append((path.name, sql, checksum))
    return result


def validate_incidents(conn: psycopg.Connection) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        columns = cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default, is_identity "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'incidents'"
        ).fetchall()
        by_name = {column["column_name"]: column for column in columns}
        types = {
            "id": {"bigint", "integer"},
            "title": {"text", "character varying"},
            "path": {"text", "character varying"},
            "symptom": {"text", "character varying"},
            "severity": {"text", "character varying"},
            "status": {"text", "character varying"},
            "detected_at": {"timestamp with time zone"},
        }
        for name, allowed in types.items():
            column = by_name.get(name)
            if not column or column["data_type"] not in allowed:
                raise SchemaError(f"Incompatible incidents.{name}; review schema manually")
            if column["is_nullable"] != "NO":
                raise SchemaError(f"incidents.{name} must be NOT NULL")
        for name in ("id", "status", "detected_at"):
            column = by_name[name]
            if column["column_default"] is None and column["is_identity"] != "YES":
                raise SchemaError(f"incidents.{name} requires a default or identity")
        for name, column in by_name.items():
            if (
                name not in types
                and column["is_nullable"] == "NO"
                and column["column_default"] is None
                and column["is_identity"] != "YES"
            ):
                raise SchemaError(f"Extra required incidents.{name} prevents inserts")
        unique_id = cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "AND a.attname = 'id' WHERE c.conrelid = 'incidents'::regclass "
            "AND c.contype IN ('p', 'u') AND c.conkey = ARRAY[a.attnum])"
        ).fetchone()
        if not unique_id["exists"]:
            raise SchemaError("incidents.id must have a primary key or unique constraint")


def check_schema(conn: psycopg.Connection) -> None:
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    expected = {version: checksum for version, _, checksum in migration_files()}
    if dict(rows) != expected:
        raise SchemaError("Schema version mismatch; run python -m ai_sre_agent.migrate")
    validate_incidents(conn)

    for table in (
        "investigations",
        "alert_occurrences",
        "workflow_checkpoints",
        "timeline_events",
        "evidence",
        "reports",
        "notification_outbox",
        "historical_documents",
        "remediation_proposals",
    ):
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            raise SchemaError(f"Required table missing: {table}")


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7824601001)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        recorded = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        migrations = migration_files()
        known = {version for version, _, _ in migrations}
        if set(recorded) - known:
            raise SchemaError("Database has unknown migrations; use matching application version")
        for version, sql, checksum in migrations:
            if version in recorded:
                if recorded[version] != checksum:
                    raise SchemaError(f"Migration checksum changed: {version}")
                continue
            conn.execute(sql)
            validate_incidents(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            applied.append(version)
        check_schema(conn)
    return applied


def main() -> int:
    try:
        settings = Settings.from_env()
        with psycopg.connect(**settings.connection_kwargs()) as conn:
            applied = apply_migrations(conn)
        print("Applied: " + ", ".join(applied) if applied else "Schema is up to date.")
        return 0
    except (ConfigurationError, SchemaError) as exc:
        print(str(exc), file=sys.stderr)
    except psycopg.Error as exc:
        print(
            f"Migration failed ({type(exc).__name__}); check database configuration.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
