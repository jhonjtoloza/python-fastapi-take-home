from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Conn
from app.main import app


@pytest.fixture
def slow_client(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app with tight limits and a test-only route whose query takes 2 seconds."""
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "statement_timeout_ms", 100)
    monkeypatch.setattr(settings, "pool_max_size", 1)
    monkeypatch.setattr(settings, "pool_timeout_s", 0.2)

    def slow(conn: Conn) -> dict[str, str]:
        conn.execute("select pg_sleep(2)")
        return {"status": "done"}

    app.add_api_route("/test-slow", slow, methods=["GET"])
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if not (isinstance(r, APIRoute) and r.path == "/test-slow")
        ]


def test_every_connection_is_open_before_the_first_request(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "pool_max_size", 3)

    with TestClient(app):
        stats = app.state.pool.get_stats()

    assert stats["pool_size"] == 3


def test_a_slow_query_is_cancelled_with_503(slow_client: TestClient) -> None:
    response = slow_client.get("/test-slow")

    assert response.status_code == 503
    assert response.json() == {"detail": "The query took too long and was cancelled."}


def test_the_connection_is_usable_after_a_cancelled_query(slow_client: TestClient) -> None:
    slow_client.get("/test-slow")

    assert slow_client.get("/records").status_code == 200


def test_an_exhausted_pool_returns_503_quickly(slow_client: TestClient) -> None:
    pool = app.state.pool
    held = pool.getconn()  # the only connection is now busy
    try:
        response = slow_client.get("/records")
    finally:
        pool.putconn(held)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "The service is busy, try again shortly."}
