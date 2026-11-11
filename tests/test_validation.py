from decimal import Decimal

import pytest

from scripts.validation import Record, header_matches, parse_row

VALID = {
    "ID": "1",
    "Age": "40",
    "Gender": "Male",
    "Country": "Germany",
    "Coffee_Intake": "3.5",
    "Caffeine_mg": "328.1",
    "Sleep_Hours": "7.5",
    "Sleep_Quality": "Good",
    "BMI": "24.9",
    "Heart_Rate": "78",
    "Stress_Level": "Low",
    "Physical_Activity_Hours": "14.5",
    "Health_Issues": "None",
    "Occupation": "Other",
    "Smoking": "0",
    "Alcohol_Consumption": "1",
}


def row(**overrides: str) -> list[str]:
    return list({**VALID, **overrides}.values())


def test_valid_row_is_parsed_without_fixes() -> None:
    parsed = parse_row(row())

    assert parsed.reasons == ()
    assert parsed.fixed is False
    assert parsed.record == Record(
        id=1,
        age=40,
        gender="Male",
        country="Germany",
        coffee_intake=Decimal("3.5"),
        caffeine_mg=Decimal("328.1"),
        sleep_hours=Decimal("7.5"),
        sleep_quality="Good",
        bmi=Decimal("24.9"),
        heart_rate=78,
        stress_level="Low",
        physical_activity_hours=Decimal("14.5"),
        health_issues="None",
        occupation="Other",
        smoking=False,
        alcohol_consumption=True,
    )


@pytest.mark.parametrize(
    ("column", "raw", "field", "expected"),
    [
        ("Gender", "  Female  ", "gender", "Female"),
        ("Gender", "\tMale  ", "gender", "Male"),
        ("Gender", "MALE", "gender", "Male"),
        ("Country", "belgium", "country", "Belgium"),
        ("Country", "  South Korea  ", "country", "South Korea"),
        ("Country", "\tUSA ", "country", "USA"),
        ("Sleep_Quality", "GOOD", "sleep_quality", "Good"),
        ("Stress_Level", "medium", "stress_level", "Medium"),
        ("Occupation", "\tHealthcare  ", "occupation", "Healthcare"),
        ("Age", " 40 ", "age", 40),
        ("Age", "4O", "age", 40),
        ("Heart_Rate", "7o", "heart_rate", 70),
        ("Coffee_Intake", "3.55", "coffee_intake", Decimal("3.6")),
        ("Smoking", "Y", "smoking", True),
        ("Smoking", "no", "smoking", False),
        ("Alcohol_Consumption", "yes", "alcohol_consumption", True),
        ("Alcohol_Consumption", "false", "alcohol_consumption", False),
    ],
)
def test_fixable_values_are_normalized_and_flagged(
    column: str, raw: str, field: str, expected: object
) -> None:
    parsed = parse_row(row(**{column: raw}))

    assert parsed.reasons == ()
    assert parsed.fixed is True
    assert parsed.record is not None
    assert getattr(parsed.record, field) == expected


@pytest.mark.parametrize(
    ("column", "raw", "reason"),
    [
        ("Age", "thirty", "Age: 'thirty' is not an integer"),
        ("Age", "O", "Age: 'O' is not an integer"),
        ("Age", "", "Age: missing value"),
        ("Age", "-3", "Age: -3 is outside 18..120"),
        ("Age", "212", "Age: 212 is outside 18..120"),
        ("Sleep_Hours", "seven", "Sleep_Hours: 'seven' is not a number"),
        ("Sleep_Hours", "-1", "Sleep_Hours: -1 is outside 0..24"),
        ("Sleep_Hours", "25", "Sleep_Hours: 25 is outside 0..24"),
        ("BMI", "n/a", "BMI: 'n/a' is not a number"),
        ("BMI", "0", "BMI: 0 is outside 10..80"),
        ("Heart_Rate", "350", "Heart_Rate: 350 is outside 25..250"),
        ("Gender", "?", "Gender: '?' is not one of Male, Female, Other"),
        ("Gender", "N/A", "Gender: 'N/A' is not one of Male, Female, Other"),
        ("Country", "Atlantis", "Country: 'Atlantis' is not a known country"),
        ("Smoking", "2", "Smoking: '2' is not a boolean (0 or 1)"),
        ("Smoking", "-1", "Smoking: '-1' is not a boolean (0 or 1)"),
        ("ID", "abc", "ID: 'abc' is not an integer"),
        ("ID", "0", "ID: 0 is outside 1..2147483647"),
    ],
)
def test_invalid_values_are_rejected_with_reason(column: str, raw: str, reason: str) -> None:
    parsed = parse_row(row(**{column: raw}))

    assert parsed.record is None
    assert parsed.reasons == (reason,)


def test_every_problem_in_a_row_is_reported() -> None:
    parsed = parse_row(row(Age="thirty", Gender="?", Smoking="2"))

    assert parsed.record is None
    assert len(parsed.reasons) == 3


@pytest.mark.parametrize(("column", "raw"), [("Age", "18"), ("Age", "120"), ("Sleep_Hours", "0")])
def test_limits_are_inclusive(column: str, raw: str) -> None:
    assert parse_row(row(**{column: raw})).record is not None


@pytest.mark.parametrize("count", [13, 15])
def test_missing_columns_are_rejected(count: int) -> None:
    parsed = parse_row(row()[:count])

    assert parsed.record is None
    assert parsed.reasons == (f"expected 16 columns, found {count}",)


@pytest.mark.parametrize("extra", [["extra"], [""], ["a", "b"]])
def test_trailing_extra_columns_are_dropped(extra: list[str]) -> None:
    parsed = parse_row(row() + extra)

    assert parsed.record == parse_row(row()).record
    assert parsed.fixed is True


def test_extra_columns_that_shift_values_are_still_rejected() -> None:
    fields = row()
    fields.insert(2, "extra")  # every value after Age moves one column to the right

    parsed = parse_row(fields)

    assert parsed.record is None
    assert parsed.reasons


def test_header_matches_ignores_surrounding_whitespace() -> None:
    assert header_matches([f" {name} " for name in VALID])
    assert not header_matches(list(VALID)[:-1])
