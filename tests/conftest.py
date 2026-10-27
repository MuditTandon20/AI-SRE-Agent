import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from ai_sre_agent.config import Settings


@pytest.fixture
def settings():
    return Settings(
        postgres_db="test", postgres_user="test", postgres_password="test-only-password"
    )


class FakePool:
    def __init__(self):
        self.opened = False
        self.closed = False
        self.error = None

    def open(self, **kwargs):
        if self.error:
            raise self.error
        self.opened = True

    def close(self):
        self.closed = True

    @contextmanager
    def connection(self):
        raise psycopg.OperationalError("sensitive database diagnostic")
        yield  # pragma: no cover


@pytest.fixture
def fake_pool():
    return FakePool()


@pytest.fixture
def isolated_database():
    """Only generated test schemas are touched; never migrate the public schema."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = "test_sre_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(
                url, autocommit=True, options=f"-c search_path={schema} -c statement_timeout=5000"
            ) as conn:
                yield url, schema, conn
        finally:
            assert schema.startswith("test_sre_") and len(schema) == 41
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def database_pool(isolated_database):
    url, schema, conn = isolated_database
    pool = ConnectionPool(
        url,
        kwargs={"options": f"-c search_path={schema} -c statement_timeout=5000"},
        min_size=1,
        max_size=2,
        timeout=2,
        open=False,
    )
    try:
        yield pool, conn
    finally:
        pool.close()
