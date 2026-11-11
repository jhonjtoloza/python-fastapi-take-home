-- One row per person. `id` is the CSV `ID`, so re-running the loader upserts
-- instead of duplicating. The loader validates first; these checks are the second line
-- of defense for writes that bypass it. Plausibility limits are documented in NOTES.md.

create table public.coffee_health (
  id integer primary key check (id > 0),
  age smallint not null check (age between 18 and 120),
  gender text not null check (gender in ('Male', 'Female', 'Other')),
  country text not null check (country <> ''),
  coffee_intake numeric(4, 1) not null check (coffee_intake between 0 and 30),
  caffeine_mg numeric(6, 1) not null check (caffeine_mg between 0 and 3000),
  sleep_hours numeric(3, 1) not null check (sleep_hours between 0 and 24),
  sleep_quality text not null
    check (sleep_quality in ('Poor', 'Fair', 'Good', 'Excellent')),
  bmi numeric(4, 1) not null check (bmi between 10 and 80),
  heart_rate smallint not null check (heart_rate between 25 and 250),
  stress_level text not null check (stress_level in ('Low', 'Medium', 'High')),
  physical_activity_hours numeric(4, 1) not null
    check (physical_activity_hours between 0 and 168),
  health_issues text not null
    check (health_issues in ('None', 'Mild', 'Moderate', 'Severe')),
  occupation text not null
    check (occupation in ('Office', 'Healthcare', 'Service', 'Student', 'Other')),
  smoking boolean not null,
  alcohol_consumption boolean not null,
  -- Lineage: the import that last wrote this row. Deliberately not a foreign key: the
  -- per-row FK check took ~40% of the insert time on a 1M-row load, the loader writes
  -- the imports row first, and imports rows are never deleted.
  import_id bigint not null
);

-- Pagination is keyset on id (the primary key). These indexes serve the
-- filters that are selective enough to benefit. Low-cardinality columns such as
-- gender or stress_level are left unindexed: a value matching a third of the table
-- or more makes a sequential scan cheaper, and every index slows down the load.
create index coffee_health_country_id_idx on public.coffee_health (country, id);
create index coffee_health_age_idx on public.coffee_health (age);
create index coffee_health_coffee_intake_idx on public.coffee_health (coffee_intake);
create index coffee_health_bmi_idx on public.coffee_health (bmi);
create index coffee_health_sleep_hours_idx on public.coffee_health (sleep_hours);

-- Combinations of low-cardinality filters can match almost nothing (stress level and
-- sleep quality are correlated). By default the planner assumes independent columns,
-- expects thousands of matches and walks the whole primary key looking for them. The
-- statistics teach it the real combinations; the index gives it a direct path.
create index coffee_health_gender_stress_sleep_idx
  on public.coffee_health (gender, stress_level, sleep_quality);
create statistics coffee_health_categories_stats (mcv)
  on country, gender, stress_level, sleep_quality
  from public.coffee_health;

-- Quarantine: every version of an ID that appears more than once with different values.
-- None of them reaches coffee_health until someone picks the right one (see NOTES.md).
create table public.duplicate_conflicts (
  like public.coffee_health including constraints,
  line_number integer not null check (line_number > 0),
  raw_line text not null,
  resolved_at timestamptz,
  primary key (import_id, line_number)
);

create index duplicate_conflicts_pending_idx
  on public.duplicate_conflicts (id)
  where resolved_at is null;
