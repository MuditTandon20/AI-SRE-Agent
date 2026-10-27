import psycopg
import pytest
from fastapi.testclient import TestClient

from ai_sre_agent.app import create_app
from ai_sre_agent.incidents import IncidentCreate, IncidentRepository
from ai_sre_agent.migrate import SchemaError, apply_migrations, check_schema

pytestmark = pytest.mark.integration


def test_full_api_persists_and_paginates(settings, database_pool):
    pool, conn = database_pool
    assert apply_migrations(conn) == ["001_incidents.sql", "002_investigation.sql"]
    assert apply_migrations(conn) == []
    app = create_app(settings, pool_factory=lambda _: pool)
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        first = client.post(
            "/incidents",
            json={
                "title": "O'Brien timeout",
                "path": "/checkout",
                "symptom": "DB busy",
                "severity": "SEV1",
            },
        )
        assert first.status_code == 201
        incident_id = first.json()["id"]
        result = client.get(f"/incidents/{incident_id}").json()
        assert result["title"] == "O'Brien timeout"
        assert result["status"] == "open"
        client.post(
            "/incidents",
            json={"title": "Second", "path": "/", "symptom": "Slow", "severity": "SEV3"},
        )
        assert client.get("/incidents?limit=1").json()["items"][0]["title"] == "Second"
        page = client.get("/incidents?limit=1&offset=1").json()
        assert page["items"][0]["id"] == incident_id
        assert client.get("/incidents/999999").status_code == 404
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2


def test_adopts_existing_table_without_losing_data(isolated_database):
    _, _, conn = isolated_database
    conn.execute(
        "CREATE TABLE incidents (id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, "
        "path TEXT NOT NULL, symptom TEXT NOT NULL, severity TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', detected_at TIMESTAMPTZ NOT NULL "
        "DEFAULT now(), resolved_at TIMESTAMPTZ)"
    )
    conn.execute(
        "INSERT INTO incidents (title,path,symptom,severity) VALUES ('existing','/','slow','SEV2')"
    )
    apply_migrations(conn)
    assert conn.execute("SELECT title, resolved_at FROM incidents").fetchall() == [
        ("existing", None)
    ]
    check_schema(conn)


def test_incompatible_schema_rolls_back_migration_bookkeeping(isolated_database):
    _, _, conn = isolated_database
    conn.execute("CREATE TABLE incidents (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO incidents VALUES ('keep-me')")
    with pytest.raises(SchemaError, match="incidents.id"):
        apply_migrations(conn)
    assert conn.execute("SELECT id FROM incidents").fetchall() == [("keep-me",)]
    assert conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None


def test_changed_migration_checksum_is_rejected(isolated_database):
    _, _, conn = isolated_database
    apply_migrations(conn)
    conn.execute("UPDATE schema_migrations SET checksum = 'modified'")
    with pytest.raises(SchemaError, match="checksum"):
        apply_migrations(conn)


def test_failed_insert_rolls_back_and_pool_remains_usable(settings, database_pool):
    pool, conn = database_pool
    apply_migrations(conn)
    conn.execute("ALTER TABLE incidents ADD CONSTRAINT reject_title CHECK (title <> 'reject')")
    pool.open(wait=True)
    repository = IncidentRepository(pool)
    with pytest.raises(psycopg.errors.CheckViolation):
        repository.create(IncidentCreate(title="reject", path="/", symptom="x", severity="SEV1"))
    repository.create(IncidentCreate(title="valid", path="/", symptom="x", severity="SEV1"))
    assert len(repository.list(20, 0)) == 1


def test_readiness_detects_missing_schema_and_schema_drift(settings, database_pool):
    pool, conn = database_pool
    with TestClient(create_app(settings, pool_factory=lambda _: pool)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        apply_migrations(conn)
        assert client.get("/ready").status_code == 200
        conn.execute("ALTER TABLE incidents ALTER COLUMN status DROP DEFAULT")
        assert client.get("/ready").status_code == 503
