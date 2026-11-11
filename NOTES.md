# Notes

```bash
make db-reset && make load && make dev   # API on http://localhost:8000/docs
make test                                # DB tests use a throwaway database
```

## Schema

| Table | Purpose |
| --- | --- |
| `coffee_health` | One row per person; `id` is the CSV `ID` and the primary key, so loads upsert instead of duplicating. |
| `imports` | One row per loader run: file name, SHA-256, status (`processing`, `completed`, `failed`), row counters, error. |
| `rejected_rows` | Rows that could not be loaded: line number, raw line, list of reasons. |
| `duplicate_conflicts` | Quarantine for IDs that appear with different values. |

- Types follow the data (`smallint`, `numeric(p,1)`, `boolean`), and every limit and category is also a `CHECK` constraint, a second line of defense behind the loader.
- `coffee_health.import_id` is deliberately not a foreign key: the per-row check took ~40% of the insert time on 1M rows, and `imports` rows are never deleted.

## Loading and cleaning

`scripts/load_data.py` streams the file. Python validates and normalizes each record (`scripts/validation.py`, pure functions) and `COPY`s it into a temporary staging table; SQL then deduplicates, quarantines and upserts. Everything runs in one transaction: a failed run leaves no partial data and its import is marked `failed`.

| Column | Limits (inclusive) | Rejected in the dataset |
| --- | --- | --- |
| Age | 18–120 | -3, 150, 212 |
| Sleep_Hours | 0–24 | -1, 25 |
| BMI | 10–80 | 0 |
| Heart_Rate | 25–250 | 0, 350 |
| Coffee_Intake / Caffeine_mg / Physical_Activity_Hours | 0–30 / 0–3000 / 0–168 | |

### Business decisions

- **Fixed and loaded** (`fixed`): whitespace and letter case in categories (`"\tMale  "`, `belgium`); booleans as words (`yes`, `Y`, `false`); a letter O typed for a zero next to a digit (`4O` → 40, line 1918); trailing extra columns (line 2659 ends with `,extra`); decimals rounded to one place. If extra columns shift the values instead, validation rejects the row.
- **Rejected** (`rejected_rows`, to fix at the source and load again): missing values and placeholders (`N/A`, `?`, `unknown`), numbers as words (`thirty`), values outside the limits, rows with fewer than 16 columns, invalid CSV, unknown countries (a closed list, so a misspelling never becomes a new filter value).
- **Same ID, same values**: an exact copy, loaded once and counted as `duplicated`. `ID` identifies a person, so a copy is not a new event.
- **Same ID, different values** (IDs 3338, 6115, 6748, 7494): no way to tell which is right, so **no version is loaded**; all go to `duplicate_conflicts` for a person to decide. A table in the same database keeps the load and the quarantine in one transaction. To promote the right version:

  ```bash
  uv run python -c "import psycopg; from app.config import settings; from scripts.load_data import PROMOTE_CONFLICT; psycopg.connect(settings.database_url, autocommit=True).execute(PROMOTE_CONFLICT, {'id': 3338, 'line_number': 3339})"
  ```

- **Re-running**: the same file (by SHA-256) is imported once; running it again prints the stored summary. A different file with known IDs updates them (newest wins). An advisory lock stops two loaders on the same file, and an import left `processing` by a dead loader is marked `failed` and retried.

Result: `read 10,012, loaded 9,973 (fixed 58), rejected 23, duplicated 8, quarantined 8`. `read = loaded + rejected + duplicated + quarantined`; the 3 blank lines are not records.

## API

`GET /records`: filters `country`, `gender`, `sleep_quality`, `stress_level` (repeat to select several), inclusive ranges `min_`/`max_` for `age`, `coffee_intake`, `bmi`, `sleep_hours`, all combined with AND. `limit` 1–100 (default 20) and `cursor`; the response is `{"items": [...], "next_cursor": ...}`.

- **Keyset pagination on `id`**: no row is repeated or skipped between pages. The cursor is opaque (base64 of the last id). No total count, which would count every match on every request.
- **422** for `min_* > max_*`, unknown categories (`gender=male`), invalid cursor, `limit` out of range and unknown parameters (`contry=Japan`).
- `GET /records/summary?group_by=country` (or `gender`, `sleep_quality`, `stress_level`, `occupation`): count and averages per group, same filters.

## Tests and code quality

85 tests, written before the code they cover. `tests/test_validation.py` (47) needs no database: it passes with `DATABASE_URL` pointing to an unreachable host. Loader (7), API (25), resilience (4) and health (2) tests run against real Postgres: each creates a throwaway database with `db/init/*.sql`, so they never touch loaded data. `make lint` passes with no `type: ignore`; the only exception is two `# noqa: E402` in `scripts/load_data.py`, which adds the project root to `sys.path` so `make load` can keep running it as a script.

## Scale

**Priority: the API stays responsive.** An import runs occasionally and can take longer; when the two compete (indexes, above all), the API wins.

### Loader

- **Constant memory**: line-by-line reading, `COPY` streaming, duplicates resolved in SQL. ~50 MB peak on 1M rows.
- **1M rows in 43 s**; about 70 minutes for 100M. Parsing is ~10 s per million; most of the rest is inserting into the indexed table. Measured with four secondary indexes; the three added later for the API were not re-measured.
- Indexes stay in place during loads (dropping them would save ~9 s per million, but load time is not critical).
- Next steps if imports must coexist with traffic: batches with a commit every N rows, or a job queue, to spread the disk pressure of one long transaction; parsing in several processes.

### API

`EXPLAIN ANALYZE` on 1M generated rows:

| Query | Time |
| --- | --- |
| Page 1, and page 40,000 with the cursor | 0.1 ms each |
| Page 40,000 with `OFFSET`, for comparison | 219 ms |
| `country=Japan` on a deep page | 0.2 ms |
| Empty `sleep_hours` range | 35 ms → 0.1 ms with its index |
| `gender` + `stress_level` + `sleep_quality` matching nothing | 170 ms → 0.1 ms |
| Summary by country | ~85 ms |

- Common filters match many rows, so walking the primary key stops after `limit` matches; every range filter has an index for the selective cases.
- Stress level and sleep quality are correlated, but Postgres assumes independent columns, expected thousands of matches and walked the whole table. A composite index alone did not change the plan; extended statistics (`create statistics ... (mcv)`) together with the index `(gender, stress_level, sleep_quality)` did.
- Low-cardinality columns alone (`gender=Female`) are fast without their own index.

### Connections under concurrent load

Synchronous endpoints run in FastAPI's thread pool; each request borrows one connection from a pool of 10. 50 concurrent clients, one API process, 1M rows:

| Traffic | Before | After |
| --- | --- | --- |
| Typical filters | 666 req/s, 0 errors | 664 req/s, p99 102 ms, 0 errors |
| Combination matching nothing | **2 req/s, 160/300 failed with 500 after 30 s** | 797 req/s, p99 78 ms, 0 errors |
| Summary (full scan each) | not measured | 9 req/s, 98/300 answered 503 |

A few slow queries held all 10 connections and every other request queued until it failed. Now:

- `statement_timeout` 3 s (`STATEMENT_TIMEOUT_MS`): slow queries are cancelled with **503**.
- Pool timeout 5 s (`POOL_TIMEOUT_S`): no free connection means **503** with `Retry-After: 1`, not a 500 after 30 s.
- All connections open at startup (`POOL_MAX_SIZE`): growing the pool under a burst caused 36 errors on a cold start; now none.
- Remaining limit: the summary at that rate needs a pre-aggregated table. Several API processes multiply connections; past `max_connections`, PgBouncer.
