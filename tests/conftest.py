from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.config import settings
from app.main import app

INIT_SQL = sorted(Path(__file__).resolve().parents[1].joinpath("db", "init").glob("*.sql"))


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app lifespan (opens the DB pool).
    with TestClient(app) as c:
        yield c


@pytest.fixture
def database_url() -> Iterator[str]:
    """A throwaway database with the real schema, so tests never touch loaded data."""
    name = f"coffee_test_{uuid4().hex[:12]}"
    with psycopg.connect(settings.database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = make_conninfo(settings.database_url, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for path in INIT_SQL:
                conn.execute(path.read_text())
        yield url
    finally:
        with psycopg.connect(settings.database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )
