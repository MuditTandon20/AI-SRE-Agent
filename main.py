from fastapi import FastAPI, Request
import time
from enum import Enum
from pydantic import BaseModel
import logging
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

pool = ConnectionPool(
    kwargs={
        "host": "localhost",
        "port": 5433,
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
    },
    min_size=1,
    max_size=5,
    timeout=3,
    open=False,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    yield
    pool.close()

app = FastAPI(lifespan=lifespan)

        # setup logging first
logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s"
        )
logger = logging.getLogger("app")

@app.middleware("http")
async def log_requests(request: Request, call_next):
            start = time.perf_counter()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                latency_ms = (time.perf_counter() - start) * 1000
                logger.info(f"method={request.method} path={request.url.path} status={status} latency_ms={latency_ms:.0f}")


@app.get("/")
def root():
            return {"message": "AI SRE Agent API is running"}


@app.get("/health")
def health():
            return {"status": "ok"}

@app.get("/crash")
def crash():
            raise Exception("Something went wrong ")


@app.get("/slow")
def slow():
            time.sleep(5)
            return {"status": "finally done"}

class Severity(str, Enum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"

class IncidentCreate(BaseModel):
    title: str
    path: str
    symptom: str
    severity: Severity

@app.post("/incidents", status_code=201)
def create_incident(incident: IncidentCreate):
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO incidents (title, path, symptom, severity) "
            "VALUES (%s, %s, %s, %s) RETURNING id, status, detected_at",
            (incident.title, incident.path, incident.symptom, incident.severity.value),
        ).fetchone()
    return {"id": row[0], "status": row[1], "detected_at": row[2]}