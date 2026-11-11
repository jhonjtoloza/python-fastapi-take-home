"""Load the coffee health CSV into Postgres.

Usage:
    uv run python scripts/load_data.py data/coffee_health_raw.csv

The file is streamed row by row, so memory use does not grow with its size:
1. Python parses, validates and normalizes each record (scripts/validation.py) and
   streams it with COPY into a temporary staging table, valid or not.
2. SQL then resolves duplicate IDs, stores rejected rows and upserts into coffee_health.
Everything from step 1 on runs in one transaction: a failed run leaves no partial data.
"""

import csv
import hashlib
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import psycopg

# Running `python scripts/load_data.py` puts scripts/ on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from scripts.validation import header_matches, parse_row  # noqa: E402

PROGRESS_EVERY = 1_000_000
REJECTED_SHOWN = 20

DATA_COLUMNS = (
    "id",
    "age",
    "gender",
    "country",
    "coffee_intake",
    "caffeine_mg",
    "sleep_hours",
    "sleep_quality",
    "bmi",
    "heart_rate",
    "stress_level",
    "physical_activity_hours",
    "health_issues",
    "occupation",
    "smoking",
    "alcohol_consumption",
)
_COLUMNS = ", ".join(DATA_COLUMNS)
_EXCLUDED = ", ".join(f"excluded.{c}" for c in DATA_COLUMNS)
_EXISTING = ", ".join(f"coffee_health.{c}" for c in DATA_COLUMNS[1:])
_EXCLUDED_VALUES = ", ".join(f"excluded.{c}" for c in DATA_COLUMNS[1:])
_EMPTY_RECORD = (None,) * len(DATA_COLUMNS)

CREATE_STAGING = f"""
create temp table staging (
  line_number integer not null,
  raw_line text not null,
  reasons text[],
  fixed boolean not null,
  {
    ", ".join(
        f"{c} {t}"
        for c, t in (
            ("id", "integer"),
            ("age", "smallint"),
            ("gender", "text"),
            ("country", "text"),
            ("coffee_intake", "numeric(4,1)"),
            ("caffeine_mg", "numeric(6,1)"),
            ("sleep_hours", "numeric(3,1)"),
            ("sleep_quality", "text"),
            ("bmi", "numeric(4,1)"),
            ("heart_rate", "smallint"),
            ("stress_level", "text"),
            ("physical_activity_hours", "numeric(4,1)"),
            ("health_issues", "text"),
            ("occupation", "text"),
            ("smoking", "boolean"),
            ("alcohol_consumption", "boolean"),
        )
    )
}
) on commit drop
"""

# IDs that appear more than once with different values: there is no way to tell which
# version is right, so every version goes to quarantine for a person to decide.
CREATE_CONFLICTS = f"""
create temp table conflicting_ids on commit drop as
select id from staging
where reasons is null
group by id
having count(distinct ({", ".join(DATA_COLUMNS[1:])})) > 1
"""

INSERT_REJECTED = """
insert into rejected_rows (import_id, line_number, raw_line, reasons)
select %(import_id)s, line_number, raw_line, reasons
from staging
where reasons is not null
"""

INSERT_QUARANTINE = f"""
insert into duplicate_conflicts ({_COLUMNS}, import_id, line_number, raw_line)
select {_COLUMNS}, %(import_id)s, line_number, raw_line
from staging
join conflicting_ids using (id)
where reasons is null
"""

# Manual resolution of a conflict: promote one quarantined version (by ID and line
# number) into coffee_health and mark every pending version of that ID as resolved.
PROMOTE_CONFLICT = f"""
with chosen as (
  select * from duplicate_conflicts
  where id = %(id)s and line_number = %(line_number)s and resolved_at is null
),
promoted as (
  insert into coffee_health ({_COLUMNS}, import_id)
  select {_COLUMNS}, import_id from chosen
  on conflict (id) do update set ({_COLUMNS}, import_id) = ({_EXCLUDED}, excluded.import_id)
)
update duplicate_conflicts
set resolved_at = now()
where id = %(id)s and resolved_at is null and exists (select 1 from chosen)
"""

# Exact copies of a record collapse into its first occurrence. Re-loading an ID that is
# already in the table updates it (the newest import wins) and skips unchanged rows.
UPSERT = f"""
with accepted as (
  select * from staging s
  where reasons is null and not exists (select 1 from conflicting_ids c where c.id = s.id)
),
unique_records as (
  select distinct on (id) * from accepted order by id, line_number
),
upserted as (
  insert into coffee_health ({_COLUMNS}, import_id)
  select {_COLUMNS}, %(import_id)s from unique_records
  on conflict (id) do update set ({_COLUMNS}, import_id) = ({_EXCLUDED}, excluded.import_id)
  where ({_EXISTING}) is distinct from ({_EXCLUDED_VALUES})
)
select
  (select count(*) from unique_records),
  (select count(*) from unique_records where fixed),
  (select count(*) from accepted) - (select count(*) from unique_records)
"""

COMPLETE_IMPORT = """
update imports
set status = 'completed', finished_at = now(), rows_read = %(read)s,
    rows_loaded = %(loaded)s, rows_fixed = %(fixed)s, rows_rejected = %(rejected)s,
    rows_duplicated = %(duplicated)s, rows_quarantined = %(quarantined)s
where id = %(import_id)s
"""


class LoaderError(Exception):
    """The file cannot be imported. The message explains why."""


@dataclass(frozen=True)
class Summary:
    import_id: int
    already_imported: bool
    rows_read: int
    rows_loaded: int
    rows_fixed: int
    rows_rejected: int
    rows_duplicated: int
    rows_quarantined: int


def load(path: Path, database_url: str) -> Summary:
    """Import `path` once. A file that was already imported returns its stored summary."""
    with path.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()

    # Bookkeeping uses its own autocommit connection, so a failed load can still be
    # recorded as failed. The advisory lock lives as long as this connection.
    with psycopg.connect(database_url, autocommit=True) as meta:
        locked = meta.execute(
            "select pg_try_advisory_lock(hashtextextended(%s, 0))", (digest,)
        ).fetchone()
        if not locked or not locked[0]:
            raise LoaderError("another loader is importing this file right now")

        previous = meta.execute(
            "select id, status, rows_read, rows_loaded, rows_fixed, rows_rejected,"
            " rows_duplicated, rows_quarantined from imports"
            " where file_sha256 = %s and status in ('processing', 'completed')",
            (digest,),
        ).fetchone()
        if previous and previous[1] == "completed":
            return Summary(previous[0], True, *previous[2:])
        if previous:
            # Holding the lock proves the loader that started it is gone.
            meta.execute(
                "update imports set status = 'failed', finished_at = now(),"
                " error = 'abandoned: the loader stopped before finishing' where id = %s",
                (previous[0],),
            )

        row = meta.execute(
            "insert into imports (file_name, file_sha256, file_size_bytes)"
            " values (%s, %s, %s) returning id",
            (path.name, digest, path.stat().st_size),
        ).fetchone()
        assert row is not None
        import_id: int = row[0]

        try:
            return _import_rows(path, database_url, import_id)
        except BaseException as exc:
            meta.execute(
                "update imports set status = 'failed', finished_at = now(), error = %s"
                " where id = %s",
                (str(exc) or type(exc).__name__, import_id),
            )
            raise


def _import_rows(path: Path, database_url: str, import_id: int) -> Summary:
    with psycopg.connect(database_url) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(CREATE_STAGING)
        read = _stage(cur, path)
        cur.execute("analyze staging")
        cur.execute(CREATE_CONFLICTS)
        cur.execute(INSERT_REJECTED, {"import_id": import_id})
        rejected = cur.rowcount
        cur.execute(INSERT_QUARANTINE, {"import_id": import_id})
        quarantined = cur.rowcount
        cur.execute(UPSERT, {"import_id": import_id})
        counts = cur.fetchone()
        assert counts is not None
        loaded, fixed, duplicated = counts
        summary = Summary(import_id, False, read, loaded, fixed, rejected, duplicated, quarantined)
        cur.execute(
            COMPLETE_IMPORT,
            {
                "import_id": import_id,
                "read": read,
                "loaded": loaded,
                "fixed": fixed,
                "rejected": rejected,
                "duplicated": duplicated,
                "quarantined": quarantined,
            },
        )
        return summary


def _stage(cur: psycopg.Cursor[tuple[object, ...]], path: Path) -> int:
    """Stream every record into the staging table. Returns the number of rows read."""
    read = 0
    started = time.monotonic()
    # errors="replace": one bad byte rejects its row instead of aborting the whole file.
    with (
        path.open(encoding="utf-8-sig", errors="replace", newline="") as f,
        cur.copy(
            f"copy staging (line_number, raw_line, reasons, fixed, {_COLUMNS}) from stdin"
        ) as copy,
    ):
        records = _read_records(f)
        header = next(records, None)
        if header is None or header[2] is None or not header_matches(header[2]):
            raise LoaderError(f"unexpected header in {path.name}")

        for line_number, raw, fields, error in records:
            if fields == []:  # blank line
                continue
            read += 1
            if fields is None:
                copy.write_row(
                    (line_number, raw, [f"malformed CSV: {error}"], False, *_EMPTY_RECORD)
                )
                continue
            parsed = parse_row(fields)
            if parsed.record is None:
                copy.write_row((line_number, raw, list(parsed.reasons), False, *_EMPTY_RECORD))
            else:
                copy.write_row((line_number, raw, None, parsed.fixed, *parsed.record))
            if read % PROGRESS_EVERY == 0:
                rate = read / (time.monotonic() - started)
                print(f"  {read:,} rows read ({rate:,.0f} rows/s)", file=sys.stderr)
    return read


def _read_records(f: TextIO) -> Iterator[tuple[int, str, list[str] | None, str | None]]:
    """Yield (first line number, raw text, fields, csv error) for each CSV record.

    `fields` is None when the record is not valid CSV; reading continues after it.
    """
    lines: list[str] = []

    def recorded() -> Iterator[str]:
        for text in f:
            lines.append(text)
            yield text

    reader = csv.reader(recorded(), strict=True)
    line_number = 1
    while True:
        fields: list[str] | None
        error: str | None = None
        try:
            fields = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            fields, error = None, str(exc)
        yield line_number, "".join(lines).rstrip("\r\n"), fields, error
        lines.clear()
        line_number = reader.line_num + 1


def _print_report(summary: Summary, database_url: str) -> None:
    if summary.already_imported:
        print(f"File already imported (import {summary.import_id}); nothing to do.")
    else:
        with psycopg.connect(database_url) as conn:
            rows = conn.execute(
                "select line_number, reasons from rejected_rows where import_id = %s"
                " order by line_number limit %s",
                (summary.import_id, REJECTED_SHOWN),
            ).fetchall()
        if rows:
            print("Rejected rows:")
            for line_number, reasons in rows:
                print(f"  line {line_number}: {'; '.join(reasons)}")
            if summary.rows_rejected > len(rows):
                print(
                    f"  ... {summary.rows_rejected - len(rows):,} more in rejected_rows"
                    f" (import_id = {summary.import_id})"
                )
    print(
        f"Import {summary.import_id}: read {summary.rows_read:,}, loaded {summary.rows_loaded:,}"
        f" (fixed {summary.rows_fixed:,}), rejected {summary.rows_rejected:,},"
        f" duplicated {summary.rows_duplicated:,}, quarantined {summary.rows_quarantined:,}"
    )
    if summary.rows_quarantined:
        print(
            "  Quarantined rows share an ID with different values; review them in"
            " duplicate_conflicts and promote the right version (see NOTES.md)."
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        summary = load(args.csv_path, settings.database_url)
    except (LoaderError, OSError) as exc:
        raise SystemExit(f"Import failed: {exc}") from exc
    _print_report(summary, settings.database_url)
    print(f"Done in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
