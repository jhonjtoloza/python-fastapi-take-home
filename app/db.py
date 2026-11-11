from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

Pool = ConnectionPool[Connection[DictRow]]


def create_pool() -> Pool:
    """Create and open a connection pool. Called once per app lifespan."""
    pool: Pool = ConnectionPool(
        settings.database_url,
        connection_class=Connection[DictRow],
        kwargs={
            "row_factory": dict_row,
            "options": f"-c statement_timeout={settings.statement_timeout_ms}",
        },
        # Open every connection at startup: growing the pool under a burst of traffic
        # made early requests wait past the pool timeout and fail with 503.
        min_size=settings.pool_max_size,
        max_size=settings.pool_max_size,
        timeout=settings.pool_timeout_s,
        open=False,
    )
    pool.open(wait=True)
    return pool


def get_conn(request: Request) -> Iterator[Connection[DictRow]]:
    """FastAPI dependency that borrows a connection from the pool for one request."""
    pool: Pool = request.app.state.pool
    with pool.connection() as conn:
        yield conn


Conn = Annotated[Connection[DictRow], Depends(get_conn)]
