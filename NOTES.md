# Notes

## How to run

```bash
make db-reset   # apply the schema in db/init to a fresh database
make load       # import data/coffee_health_raw.csv
make dev        # API on http://localhost:8000/docs
make test       # loader tests use a throwaway database; your data is not touched
```

## Schema

| Table | Purpose |
| --- | --- |
| `coffee_health` | One row per person. `id` is the CSV `ID` and the primary key, so loads upsert instead of duplicating. |
| `imports` | One row per loader run: file name, SHA-256, size, status (`processing`, `completed`, `failed`), row counters, error. |
| `rejected_rows` | Rows that could not be loaded: import, line number, raw line, list of reasons. |
| `duplicate_conflicts` | Quarantine for IDs that appear more than once with different values (see below). |

- Types follow the data: `smallint` for age and heart rate, `numeric(p,1)` for decimals (no float rounding), `boolean` for smoking and alcohol.
- Every limit and category is also a `CHECK` constraint. The loader validates first; the database is a second line of defense for writes that bypass it.
- `coffee_health.import_id` records which import last wrote the row. It is deliberately **not a foreign key**: on a 1M-row load the per-row FK check took about 40% of the insert time, the loader always writes the `imports` row first, and `imports` rows are never deleted.

## Loading and cleaning

The loader (`scripts/load_data.py`) streams the file:

1. Python parses, validates and normalizes each record (`scripts/validation.py`, pure functions, unit tested without a database) and streams every record, valid or not, into a temporary staging table with `COPY ... FROM STDIN`.
2. SQL resolves duplicate IDs, stores rejected and quarantined rows, and upserts into `coffee_health`.

Steps 1 and 2 run in one transaction: a failed run leaves no partial data, and its `imports` row is marked `failed` with the error.

### Plausibility limits (inclusive)

| Column | Limits | Rejected examples in the dataset |
| --- | --- | --- |
| Age | 18–120 (adults) | -3, 150, 212 |
| Sleep_Hours | 0–24 | -1, 25 |
| BMI | 10–80 | 0 |
| Heart_Rate | 25–250 bpm | 0, 350 |
| Coffee_Intake | 0–30 cups/day | |
| Caffeine_mg | 0–3000 mg/day | |
| Physical_Activity_Hours | 0–168 h/week | |

### Business decisions

Several cases have no single right answer. These are the choices made, and why.

**Fixed and loaded** (counted as `fixed`):

- Surrounding whitespace, tabs and letter case in categories: `"\tMale  "`, `MALE`, `belgium`, `GOOD` become `Male`, `Belgium`, `Good`.
- Booleans written as words: `yes`, `Y`, `true` become `1`; `no`, `false` become `0`.
- A letter O typed for a zero in an integer, when the value also has a real digit: `4O` becomes `40` (line 1918). A lone `O` is still rejected.
- Trailing extra columns are dropped: line 2659 ends with `,extra` and its first 16 values are valid. If an extra column shifted the values instead, those values fail validation and the row is rejected.
- Decimals with more than one decimal place are rounded half up to one decimal.

**Rejected** (stored in `rejected_rows`, to be fixed at the source and loaded again):

- Missing values, placeholders such as `N/A`, `n/a`, `?` or `unknown`, and numbers written as words (`thirty`, `seven`).
- Values outside the limits above.
- Rows with fewer than 16 columns: there is no way to know which values are missing.
- Countries outside the known list. An unknown spelling would otherwise become a new filter value; adding a real country is a one-line change in `scripts/validation.py`.
- Records that are not valid CSV (for example an unclosed quote) and booleans other than the spellings above (`-1`, `2`).

**Duplicate IDs:**

- **Same ID, same values**: an exact copy. Loaded once and counted as `duplicated`. `ID` identifies a person, so a copy carries no new information; it is not an event to be counted.
- **Same ID, different values** (IDs 3338, 6115, 6748, 7494: age and heart rate differ): there is no way to tell which version is correct. **No version is loaded.** All of them go to `duplicate_conflicts` for a person to review. Rejecting them would lose the choice; picking one automatically (first or last) would publish a guess.

To promote the right version after review (here, line 3339 for ID 3338):

```sql
-- Same statement as PROMOTE_CONFLICT in scripts/load_data.py
with chosen as (
  select * from duplicate_conflicts
  where id = 3338 and line_number = 3339 and resolved_at is null
),
promoted as (
  insert into coffee_health (id, age, gender, country, coffee_intake, caffeine_mg,
    sleep_hours, sleep_quality, bmi, heart_rate, stress_level, physical_activity_hours,
    health_issues, occupation, smoking, alcohol_consumption, import_id)
  select id, age, gender, country, coffee_intake, caffeine_mg, sleep_hours,
    sleep_quality, bmi, heart_rate, stress_level, physical_activity_hours,
    health_issues, occupation, smoking, alcohol_consumption, import_id
  from chosen
  on conflict (id) do update set (age, gender, country, coffee_intake, caffeine_mg,
    sleep_hours, sleep_quality, bmi, heart_rate, stress_level, physical_activity_hours,
    health_issues, occupation, smoking, alcohol_consumption, import_id)
  = (excluded.age, excluded.gender, excluded.country, excluded.coffee_intake,
    excluded.caffeine_mg, excluded.sleep_hours, excluded.sleep_quality, excluded.bmi,
    excluded.heart_rate, excluded.stress_level, excluded.physical_activity_hours,
    excluded.health_issues, excluded.occupation, excluded.smoking,
    excluded.alcohol_consumption, excluded.import_id)
)
update duplicate_conflicts set resolved_at = now()
where id = 3338 and resolved_at is null and exists (select 1 from chosen);
```

A separate table in the same database was chosen over a separate database: moving rows between Postgres databases needs `dblink` or a foreign data wrapper, and the load and the quarantine would no longer commit atomically.

**Re-running:**

- The same file (same SHA-256) is imported once. Running it again prints the stored summary and changes nothing.
- A different file containing IDs that are already loaded updates those rows: the newest import wins. Unchanged rows are not rewritten.
- If a loader dies mid-run, its import stays `processing`. The next run detects it through a Postgres advisory lock (held only while a loader is alive), marks it `failed` and retries. Two loaders started on the same file at the same time cannot both run.

### Result on the provided dataset

```
read 10,012, loaded 9,973 (fixed 58), rejected 23, duplicated 8, quarantined 8
```

`read = loaded + rejected + duplicated + quarantined`. Blank lines (3) are not records and are not counted. `select count(*) from coffee_health` returns 9,973.

## Scale

### Loader

- **Memory is constant**: the file is read line by line and streamed with `COPY`; duplicates are resolved in SQL, not in a Python dictionary. Peak memory was ~50 MB for 1M rows.
- **Measured on 1,001,200 rows** (the dataset repeated with new IDs, Postgres in Docker on a laptop): 43 s in total. Parsing in Python takes ~10 s per million rows; most of the rest is the insert into `coffee_health` and its indexes.
- **Extrapolated to 100M rows**: roughly 70 minutes, with the same memory. The staging table needs about twice the CSV size in temporary disk space while the load runs.
- `EXPLAIN ANALYZE` drove one change: the foreign key on `import_id` was removed (41 s to 25 s for the insert). Raising `work_mem` to avoid the on-disk sort made no measurable difference, so it was left at the default.
- Not done, would be the next steps at that size: drop the secondary indexes for an initial load and rebuild them afterwards (~9 s per million rows), parse in several processes, and avoid storing the raw line of valid rows in staging.

### API

_Pending (Part 2): filtering, keyset pagination on `id`, index usage and connection pool behavior._
