from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
