from psycopg_pool import ConnectionPool

from ai_sre_agent.config import Settings


def create_pool(settings: Settings) -> ConnectionPool:
    return ConnectionPool(
        kwargs=settings.connection_kwargs(),
        min_size=1,
        max_size=5,
        timeout=settings.database_timeout_seconds,
        check=ConnectionPool.check_connection,
        open=False,
    )
