"""Endpoints that serve the coffee health records."""

import base64
import binascii
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Query
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db import Conn
from app.domain import (
    Country,
    Gender,
    HealthIssues,
    Occupation,
    SleepQuality,
    StressLevel,
)

router = APIRouter(prefix="/records", tags=["records"])

MAX_PAGE_SIZE = 100
RANGE_FILTERS = ("age", "coffee_intake", "bmi", "sleep_hours")
CHOICE_FILTERS = ("country", "gender", "sleep_quality", "stress_level")
GroupBy = Literal["country", "gender", "sleep_quality", "stress_level", "occupation"]


class Record(BaseModel):
    id: int
    age: int
    gender: Gender
    country: Country
    coffee_intake: float
    caffeine_mg: float
    sleep_hours: float
    sleep_quality: SleepQuality
    bmi: float
    heart_rate: int
    stress_level: StressLevel
    physical_activity_hours: float
    health_issues: HealthIssues
    occupation: Occupation
    smoking: bool
    alcohol_consumption: bool


RECORD_COLUMNS = sql.SQL(", ").join(sql.Identifier(name) for name in Record.model_fields)


class RecordPage(BaseModel):
    items: list[Record]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )


class SummaryRow(BaseModel):
    group: str
    count: int
    avg_sleep_hours: float
    avg_coffee_intake: float
    avg_bmi: float


class RecordFilters(BaseModel):
    """Choice filters accept several values (OR); ranges are inclusive; all combine with AND."""

    model_config = ConfigDict(extra="forbid")

    country: list[Country] = []
    gender: list[Gender] = []
    sleep_quality: list[SleepQuality] = []
    stress_level: list[StressLevel] = []
    min_age: int | None = Field(None, ge=0)
    max_age: int | None = Field(None, ge=0)
    min_coffee_intake: float | None = Field(None, ge=0)
    max_coffee_intake: float | None = Field(None, ge=0)
    min_bmi: float | None = Field(None, ge=0)
    max_bmi: float | None = Field(None, ge=0)
    min_sleep_hours: float | None = Field(None, ge=0)
    max_sleep_hours: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def check_ranges(self) -> Self:
        for name in RANGE_FILTERS:
            low, high = getattr(self, f"min_{name}"), getattr(self, f"max_{name}")
            if low is not None and high is not None and low > high:
                raise ValueError(f"min_{name} must not be greater than max_{name}")
        return self

    def where(self) -> tuple[sql.Composable, dict[str, object]]:
        clauses: list[sql.Composable] = []
        params: dict[str, object] = {}
        for name in CHOICE_FILTERS:
            if values := getattr(self, name):
                clauses.append(
                    sql.SQL("{} = any({})").format(sql.Identifier(name), sql.Placeholder(name))
                )
                params[name] = values
        for name in RANGE_FILTERS:
            for bound, operator in (("min", ">="), ("max", "<=")):
                key = f"{bound}_{name}"
                if (value := getattr(self, key)) is not None:
                    clauses.append(
                        sql.SQL("{} {} {}").format(
                            sql.Identifier(name), sql.SQL(operator), sql.Placeholder(key)
                        )
                    )
                    params[key] = value
        return sql.SQL(" and ").join(clauses or [sql.SQL("true")]), params


class RecordQuery(RecordFilters):
    limit: int = Field(20, ge=1, le=MAX_PAGE_SIZE)
    cursor: str | None = Field(None, description="`next_cursor` from the previous page.")

    @field_validator("cursor")
    @classmethod
    def check_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            decode_cursor(value)
        return value


class SummaryQuery(RecordFilters):
    group_by: GroupBy


def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(str(last_id).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    """The cursor is opaque to clients: today it is the last id seen, base64-encoded."""
    try:
        last_id = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError):
        raise ValueError("invalid cursor") from None
    if last_id < 0:
        raise ValueError("invalid cursor")
    return last_id


@router.get("")
def list_records(conn: Conn, query: Annotated[RecordQuery, Query()]) -> RecordPage:
    """Filtered records, ordered by id, paginated with a keyset cursor.

    Every page costs the same: `id > last_id` seeks the primary key index instead of
    skipping rows like OFFSET does. There is no total count, which would need a full
    count of the matching rows on every request.
    """
    where, params = query.where()
    if query.cursor is not None:
        where = sql.SQL("{} and id > %(after_id)s").format(where)
        params["after_id"] = decode_cursor(query.cursor)
    params["limit"] = query.limit + 1  # one extra row tells whether there is a next page
    rows = conn.execute(
        sql.SQL("select {} from coffee_health where {} order by id limit %(limit)s").format(
            RECORD_COLUMNS, where
        ),
        params,
    ).fetchall()
    items = [Record.model_validate(row) for row in rows[: query.limit]]
    has_more = len(rows) > query.limit
    return RecordPage(items=items, next_cursor=encode_cursor(items[-1].id) if has_more else None)


@router.get("/summary")
def summarize_records(conn: Conn, query: Annotated[SummaryQuery, Query()]) -> list[SummaryRow]:
    """Count and averages per group, with the same filters as /records."""
    where, params = query.where()
    rows = conn.execute(
        sql.SQL(
            "select {group} as group, count(*) as count,"
            " round(avg(sleep_hours), 2) as avg_sleep_hours,"
            " round(avg(coffee_intake), 2) as avg_coffee_intake,"
            " round(avg(bmi), 2) as avg_bmi"
            " from coffee_health where {where} group by 1 order by 1"
        ).format(group=sql.Identifier(query.group_by), where=where),
        params,
    ).fetchall()
    return [SummaryRow.model_validate(row) for row in rows]
