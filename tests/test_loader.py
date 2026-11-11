import hashlib
from pathlib import Path

import psycopg
import pytest

from scripts.load_data import PROMOTE_CONFLICT, LoaderError, load

HEADER = (
    "ID,Age,Gender,Country,Coffee_Intake,Caffeine_mg,Sleep_Hours,Sleep_Quality,BMI,"
    "Heart_Rate,Stress_Level,Physical_Activity_Hours,Health_Issues,Occupation,Smoking,"
    "Alcohol_Consumption"
)


def line(id_: int, age: str = "40", gender: str = "Male") -> str:
    return f"{id_},{age},{gender},Germany,3.5,328.1,7.5,Good,24.9,78,Low,14.5,None,Other,0,0"


def write_csv(tmp_path: Path, *lines: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text("﻿" + "\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    return path


def fetch(url: str, query: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(query).fetchall()


def test_load_cleans_rejects_and_deduplicates(tmp_path: Path, database_url: str) -> None:
    path = write_csv(
        tmp_path,
        line(1),  # line 2: valid
        line(2, gender="  male "),  # line 3: fixed
        line(3, age="thirty"),  # line 4: invalid
        "",  # line 5: blank, ignored
        line(1),  # line 6: exact copy of line 2
        line(4, age="26"),  # line 7: same ID as line 8, different values
        line(4, age="33"),  # line 8
        "5,37,Female,France,2.1,202.4,7.6,Good,17.5,80,Low,3.6,None",  # line 9: 13 columns
    )

    summary = load(path, database_url)

    assert summary.already_imported is False
    assert (summary.rows_read, summary.rows_loaded, summary.rows_fixed) == (7, 2, 1)
    assert (summary.rows_rejected, summary.rows_duplicated, summary.rows_quarantined) == (2, 1, 2)
    assert fetch(database_url, "select id, gender from coffee_health order by id") == [
        (1, "Male"),
        (2, "Male"),
    ]
    rejected = fetch(
        database_url, "select line_number, raw_line, reasons from rejected_rows order by 1"
    )
    assert rejected == [
        (4, line(3, age="thirty"), ["Age: 'thirty' is not an integer"]),
        (
            9,
            "5,37,Female,France,2.1,202.4,7.6,Good,17.5,80,Low,3.6,None",
            ["expected 16 columns, found 13"],
        ),
    ]
    assert fetch(
        database_url,
        "select line_number, raw_line, id, age, resolved_at from duplicate_conflicts order by 1",
    ) == [
        (7, line(4, age="26"), 4, 26, None),
        (8, line(4, age="33"), 4, 33, None),
    ]
    assert fetch(
        database_url,
        "select status, file_sha256, rows_read, rows_loaded, rows_fixed, rows_rejected,"
        " rows_duplicated, rows_quarantined from imports",
    ) == [("completed", hashlib.sha256(path.read_bytes()).hexdigest(), 7, 2, 1, 2, 1, 2)]


def test_a_quarantined_version_can_be_promoted(tmp_path: Path, database_url: str) -> None:
    load(write_csv(tmp_path, line(4, age="26"), line(4, age="33")), database_url)

    with psycopg.connect(database_url) as conn:
        conn.execute(PROMOTE_CONFLICT, {"id": 4, "line_number": 3})

    assert fetch(database_url, "select id, age from coffee_health") == [(4, 33)]
    assert fetch(
        database_url,
        "select line_number, resolved_at is not null from duplicate_conflicts order by 1",
    ) == [(2, True), (3, True)]


def test_running_the_same_file_twice_does_not_duplicate(tmp_path: Path, database_url: str) -> None:
    path = write_csv(tmp_path, line(1), line(2))

    first = load(path, database_url)
    second = load(path, database_url)

    assert second.already_imported is True
    assert second.import_id == first.import_id
    assert second.rows_loaded == first.rows_loaded == 2
    assert fetch(database_url, "select count(*) from coffee_health") == [(2,)]
    assert fetch(database_url, "select count(*) from imports") == [(1,)]


def test_a_changed_file_updates_existing_ids(tmp_path: Path, database_url: str) -> None:
    load(write_csv(tmp_path, line(1), line(2), name="a.csv"), database_url)

    summary = load(write_csv(tmp_path, line(2, age="50"), line(3), name="b.csv"), database_url)

    assert summary.rows_loaded == 2
    assert fetch(database_url, "select id, age from coffee_health order by id") == [
        (1, 40),
        (2, 50),
        (3, 40),
    ]


def test_unclosed_quote_is_rejected_as_malformed_csv(tmp_path: Path, database_url: str) -> None:
    path = write_csv(tmp_path, line(1), '2,"40,Male')

    summary = load(path, database_url)

    assert (summary.rows_loaded, summary.rows_rejected) == (1, 1)
    assert fetch(database_url, "select line_number, reasons from rejected_rows") == [
        (3, ["malformed CSV: unexpected end of data"])
    ]


def test_wrong_header_fails_the_import_without_loading(tmp_path: Path, database_url: str) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("id,name\n1,x\n", encoding="utf-8")

    with pytest.raises(LoaderError, match="unexpected header"):
        load(path, database_url)

    assert fetch(database_url, "select status, error is not null from imports") == [
        ("failed", True)
    ]
    assert fetch(database_url, "select count(*) from coffee_health") == [(0,)]


def test_an_abandoned_import_is_retried(tmp_path: Path, database_url: str) -> None:
    path = write_csv(tmp_path, line(1))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "insert into imports (file_name, file_sha256, file_size_bytes) values ('x', %s, 1)",
            (digest,),
        )

    summary = load(path, database_url)

    assert summary.rows_loaded == 1
    assert fetch(database_url, "select status from imports order by id") == [
        ("failed",),
        ("completed",),
    ]
