-- Import bookkeeping: one row per loader run, plus the rows it rejected.

create table public.imports (
  id bigserial primary key,
  file_name text not null,
  file_sha256 text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),
  file_size_bytes bigint not null check (file_size_bytes >= 0),
  status text not null default 'processing'
    check (status in ('processing', 'completed', 'failed')),
  rows_read integer not null default 0,
  rows_loaded integer not null default 0,
  rows_fixed integer not null default 0,
  rows_rejected integer not null default 0,
  rows_duplicated integer not null default 0,
  rows_quarantined integer not null default 0,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  check ((status = 'processing') = (finished_at is null))
);

-- A file can be processing or completed only once. Failed runs do not block a retry,
-- and two loaders started on the same file cannot both register.
create unique index imports_active_file_uq
  on public.imports (file_sha256)
  where status in ('processing', 'completed');

create table public.rejected_rows (
  id bigserial primary key,
  import_id bigint not null references public.imports (id) on delete cascade,
  line_number integer not null check (line_number > 0),
  raw_line text not null,
  reasons text[] not null check (cardinality(reasons) > 0),
  unique (import_id, line_number)
);
