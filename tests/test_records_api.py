from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

# id, age, gender, country, coffee_intake, sleep_hours, sleep_quality, bmi, stress_level
PEOPLE = [
    (1, 25, "Male", "Germany", "1.0", "8.0", "Good", "22.0", "Low"),
    (2, 40, "Female", "Japan", "3.0", "6.0", "Fair", "27.0", "Medium"),
    (3, 60, "Other", "Germany", "5.0", "5.0", "Poor", "31.0", "High"),
    (4, 33, "Female", "Germany", "2.0", "7.0", "Excellent", "19.0", "Low"),
    (5, 50, "Male", "USA", "0.0", "7.5", "Good", "24.0", "Low"),
]


@pytest.fixture
def api(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app pointed at a throwaway database seeded with PEOPLE."""
    with psycopg.connect(database_url) as conn:
        conn.cursor().executemany(
            "insert into coffee_health (id, age, gender, country, coffee_intake, caffeine_mg,"
            " sleep_hours, sleep_quality, bmi, heart_rate, stress_level,"
            " physical_activity_hours, health_issues, occupation, smoking,"
            " alcohol_consumption, import_id)"
            " values (%s, %s, %s, %s, %s, 100, %s, %s, %s, 70, %s, 5, 'None', 'Office',"
            " false, false, 1)",
            PEOPLE,
        )
    monkeypatch.setattr(settings, "database_url", database_url)
    with TestClient(app) as client:
        yield client


def ids(response: Any) -> list[int]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def error_messages(response: Any) -> str:
    assert response.status_code == 422, response.text
    return " ".join(error["msg"] for error in response.json()["detail"])


def test_records_are_returned_ordered_by_id(api: TestClient) -> None:
    response = api.get("/records")

    assert ids(response) == [1, 2, 3, 4, 5]
    assert response.json()["next_cursor"] is None
    assert response.json()["items"][1] == {
        "id": 2,
        "age": 40,
        "gender": "Female",
        "country": "Japan",
        "coffee_intake": 3.0,
        "caffeine_mg": 100.0,
        "sleep_hours": 6.0,
        "sleep_quality": "Fair",
        "bmi": 27.0,
        "heart_rate": 70,
        "stress_level": "Medium",
        "physical_activity_hours": 5.0,
        "health_issues": "None",
        "occupation": "Office",
        "smoking": False,
        "alcohol_consumption": False,
    }


def test_cursor_pagination_walks_every_record_once(api: TestClient) -> None:
    first = api.get("/records", params={"limit": 2})
    second = api.get("/records", params={"limit": 2, "cursor": first.json()["next_cursor"]})
    third = api.get("/records", params={"limit": 2, "cursor": second.json()["next_cursor"]})

    assert (ids(first), ids(second), ids(third)) == ([1, 2], [3, 4], [5])
    assert third.json()["next_cursor"] is None


def test_pagination_keeps_the_filters(api: TestClient) -> None:
    first = api.get("/records", params={"country": "Germany", "limit": 2})
    second = api.get(
        "/records",
        params={"country": "Germany", "limit": 2, "cursor": first.json()["next_cursor"]},
    )

    assert (ids(first), ids(second)) == ([1, 3], [4])


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"country": ["Germany", "Japan"]}, [1, 2, 3, 4]),
        ({"gender": "Female"}, [2, 4]),
        ({"gender": ["Male", "Other"]}, [1, 3, 5]),
        ({"sleep_quality": ["Good", "Excellent"]}, [1, 4, 5]),
        ({"stress_level": "Low", "country": "Germany"}, [1, 4]),
        ({"min_age": 40, "max_age": 50}, [2, 5]),
        ({"min_age": 40, "max_age": 40}, [2]),
        ({"min_coffee_intake": 2, "max_coffee_intake": 3}, [2, 4]),
        ({"max_bmi": 22}, [1, 4]),
        ({"min_sleep_hours": 7.5}, [1, 5]),
        ({"gender": "Female", "min_age": 35}, [2]),
        ({"country": "Brazil"}, []),
    ],
)
def test_filters(api: TestClient, params: dict[str, Any], expected: list[int]) -> None:
    assert ids(api.get("/records", params=params)) == expected


def test_min_greater_than_max_is_rejected(api: TestClient) -> None:
    response = api.get("/records", params={"min_age": 50, "max_age": 30})

    assert "min_age must not be greater than max_age" in error_messages(response)


@pytest.mark.parametrize(
    "params",
    [
        {"gender": "male"},
        {"country": "Atlantis"},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-a-cursor"},
        {"min_age": "old"},
        {"contry": "Germany"},
    ],
)
def test_invalid_parameters_are_rejected(api: TestClient, params: dict[str, Any]) -> None:
    assert api.get("/records", params=params).status_code == 422


def test_summary_groups_with_the_same_filters(api: TestClient) -> None:
    response = api.get("/records/summary", params={"group_by": "country", "max_age": 55})

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "group": "Germany",
            "count": 2,
            "avg_sleep_hours": 7.5,
            "avg_coffee_intake": 1.5,
            "avg_bmi": 20.5,
        },
        {
            "group": "Japan",
            "count": 1,
            "avg_sleep_hours": 6.0,
            "avg_coffee_intake": 3.0,
            "avg_bmi": 27.0,
        },
        {
            "group": "USA",
            "count": 1,
            "avg_sleep_hours": 7.5,
            "avg_coffee_intake": 0.0,
            "avg_bmi": 24.0,
        },
    ]


def test_summary_rejects_unknown_grouping(api: TestClient) -> None:
    assert api.get("/records/summary", params={"group_by": "bmi"}).status_code == 422
