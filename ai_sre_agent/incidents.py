from datetime import datetime
from enum import Enum

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"


class IncidentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=300)
    path: str = Field(min_length=1, max_length=2048)
    symptom: str = Field(min_length=1, max_length=10000)
    severity: Severity
    service: str = Field(default="demo-service", pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")
    environment: str = Field(default="local", pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")


class IncidentCreated(BaseModel):
    id: int
    status: str
    detected_at: datetime


class Incident(IncidentCreated):
    title: str
    path: str
    symptom: str
    severity: Severity
    service: str = "demo-service"
    environment: str = "local"


class IncidentPage(BaseModel):
    items: list[Incident]
    limit: int
    offset: int


class IncidentRepository:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def create(self, incident: IncidentCreate) -> dict:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "INSERT INTO incidents (title, path, symptom, severity, service, environment) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, status, detected_at",
                (
                    incident.title,
                    incident.path,
                    incident.symptom,
                    incident.severity.value,
                    incident.service,
                    incident.environment,
                ),
            ).fetchone()

    def get(self, incident_id: int) -> dict | None:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id, title, path, symptom, severity, status, detected_at, service, environment "
                "FROM incidents WHERE id = %s",
                (incident_id,),
            ).fetchone()

    def list(self, limit: int, offset: int) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id, title, path, symptom, severity, status, detected_at, service, environment "
                "FROM incidents ORDER BY id DESC LIMIT %s OFFSET %s",
                (limit, offset),
            ).fetchall()
