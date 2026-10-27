"""Run with python -m ai_sre_agent.worker [--once]."""

import argparse
import logging
import os
import signal
import threading
import time
from pathlib import Path

import psycopg

from ai_sre_agent.config import Settings
from ai_sre_agent.database import create_pool
from ai_sre_agent.migrate import check_schema
from ai_sre_agent.store import LeaseLost, Store
from ai_sre_agent.workflow import investigate

logger = logging.getLogger("ai_sre_agent.worker")


def run_once(store, settings, connectors=None):
    job = store.claim(settings)
    if not job:
        return False
    try:
        report = investigate(job, store, settings, connectors)
        store.finish(job, report, settings)
        logger.info("investigation_completed id=%s outcome=%s", job["id"], report.analysis.outcome)
    except LeaseLost:
        logger.warning("stale_worker_fenced id=%s", job["id"])
    except Exception as exc:
        # Errors from providers can contain credentials; persist only the category.
        try:
            store.fail(
                job,
                type(exc).__name__,
                settings,
                retryable=isinstance(exc, psycopg.OperationalError),
            )
        except LeaseLost:
            pass
        logger.warning("investigation_failed id=%s error_type=%s", job["id"], type(exc).__name__)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = Settings.from_env()
    from ai_sre_agent.telemetry import configure_tracing

    configure_tracing(settings)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    with create_pool(settings) as pool:
        pool.wait(settings.database_startup_timeout_seconds)
        with pool.connection() as conn:
            check_schema(conn)
        store = Store(pool)
        from ai_sre_agent.notifications import dispatch_once

        while not stopped.is_set():
            Path(os.environ.get("WORKER_HEARTBEAT_FILE", ".demo/worker-heartbeat")).parent.mkdir(
                parents=True, exist_ok=True
            )
            Path(os.environ.get("WORKER_HEARTBEAT_FILE", ".demo/worker-heartbeat")).write_text(
                str(time.time())
            )
            try:
                processed = run_once(store, settings)
                dispatch_once(store, settings)
            except psycopg.Error as exc:
                logger.warning("worker_database_unavailable error_type=%s", type(exc).__name__)
                processed = False
            if args.once:
                break
            if not processed:
                stopped.wait(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
