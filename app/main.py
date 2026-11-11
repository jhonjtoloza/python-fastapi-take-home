from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.errors import QueryCanceled
from psycopg_pool import PoolTimeout
from pydantic import BaseModel

from app import records
from app.db import Conn, create_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = create_pool()
    yield
    app.state.pool.close()


app = FastAPI(title="Coffee Health API", lifespan=lifespan)
app.include_router(records.router)


# Overload fails fast with 503 instead of hanging and ending in a 500, so clients can
# back off and retry while the rest of the API keeps answering.
@app.exception_handler(QueryCanceled)
def query_cancelled(request: Request, exc: QueryCanceled) -> JSONResponse:
    return JSONResponse(
        status_code=503, content={"detail": "The query took too long and was cancelled."}
    )


@app.exception_handler(PoolTimeout)
def pool_exhausted(request: Request, exc: PoolTimeout) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "The service is busy, try again shortly."},
        headers={"Retry-After": "1"},
    )


class Health(BaseModel):
    status: str
    database: str


class Note(BaseModel):
    id: int
    title: str


@app.get("/health")
def health(conn: Conn) -> Health:
    conn.execute("select 1")
    return Health(status="ok", database="ok")


@app.get("/notes")
def list_notes(conn: Conn) -> list[Note]:
    """Example endpoint backed by the sample `notes` table. Keep, replace or remove it."""
    rows = conn.execute("select id, title from notes order by id").fetchall()
    return [Note.model_validate(row) for row in rows]
