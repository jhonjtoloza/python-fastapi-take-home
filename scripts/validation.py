"""Parse, validate and normalize one CSV record. Pure functions: no I/O, no database."""

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from app.domain import (
    COUNTRIES,
    GENDERS,
    HEALTH_ISSUES,
    OCCUPATIONS,
    SLEEP_QUALITIES,
    STRESS_LEVELS,
)

COLUMNS = (
    "ID",
    "Age",
    "Gender",
    "Country",
    "Coffee_Intake",
    "Caffeine_mg",
    "Sleep_Hours",
    "Sleep_Quality",
    "BMI",
    "Heart_Rate",
    "Stress_Level",
    "Physical_Activity_Hours",
    "Health_Issues",
    "Occupation",
    "Smoking",
    "Alcohol_Consumption",
)

# Plausibility limits, inclusive. Keep in sync with the CHECK constraints in db/init.
MAX_ID = 2_147_483_647
AGE_RANGE = (18, 120)
HEART_RATE_RANGE = (25, 250)
COFFEE_INTAKE_RANGE = (Decimal(0), Decimal(30))
CAFFEINE_MG_RANGE = (Decimal(0), Decimal(3000))
SLEEP_HOURS_RANGE = (Decimal(0), Decimal(24))
BMI_RANGE = (Decimal(10), Decimal(80))
PHYSICAL_ACTIVITY_RANGE = (Decimal(0), Decimal(168))

# "0" and "1" are the canonical spellings; the others are accepted and counted as fixed.
BOOLEANS = {
    "0": False,
    "1": True,
    "false": False,
    "true": True,
    "no": False,
    "yes": True,
    "n": False,
    "y": True,
}

_INTEGER = re.compile(r"[+-]?\d+")
_DECIMAL = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")
_ONE_DECIMAL = Decimal("0.1")


class Record(NamedTuple):
    """A valid, normalized record. Field order matches the coffee_health columns."""

    id: int
    age: int
    gender: str
    country: str
    coffee_intake: Decimal
    caffeine_mg: Decimal
    sleep_hours: Decimal
    sleep_quality: str
    bmi: Decimal
    heart_rate: int
    stress_level: str
    physical_activity_hours: Decimal
    health_issues: str
    occupation: str
    smoking: bool
    alcohol_consumption: bool


class ParsedRow(NamedTuple):
    """`record` is None when `reasons` is not empty. `fixed` means a value was normalized."""

    record: Record | None
    reasons: tuple[str, ...]
    fixed: bool


def header_matches(fields: list[str]) -> bool:
    return tuple(field.strip() for field in fields) == COLUMNS


def parse_row(fields: list[str]) -> ParsedRow:
    if len(fields) < len(COLUMNS):
        return ParsedRow(None, (f"expected {len(COLUMNS)} columns, found {len(fields)}",), False)
    p = _RowParser(fields[: len(COLUMNS)])
    # Trailing extra columns are dropped. If they shifted the values instead, the
    # 16 kept columns fail validation and the row is still rejected.
    p.fixed = len(fields) > len(COLUMNS)
    record = Record(
        id=p.integer(0, (1, MAX_ID)),
        age=p.integer(1, AGE_RANGE),
        gender=p.choice(2, GENDERS),
        country=p.choice(3, COUNTRIES, unknown="is not a known country"),
        coffee_intake=p.decimal(4, COFFEE_INTAKE_RANGE),
        caffeine_mg=p.decimal(5, CAFFEINE_MG_RANGE),
        sleep_hours=p.decimal(6, SLEEP_HOURS_RANGE),
        sleep_quality=p.choice(7, SLEEP_QUALITIES),
        bmi=p.decimal(8, BMI_RANGE),
        heart_rate=p.integer(9, HEART_RATE_RANGE),
        stress_level=p.choice(10, STRESS_LEVELS),
        physical_activity_hours=p.decimal(11, PHYSICAL_ACTIVITY_RANGE),
        health_issues=p.choice(12, HEALTH_ISSUES),
        occupation=p.choice(13, OCCUPATIONS),
        smoking=p.boolean(14),
        alcohol_consumption=p.boolean(15),
    )
    if p.reasons:
        return ParsedRow(None, tuple(p.reasons), p.fixed)
    return ParsedRow(record, (), p.fixed)


class _RowParser:
    """Parses fields one by one, collecting every problem instead of stopping at the first.

    Invalid fields return a placeholder of the right type; `parse_row` discards the
    record when any reason was collected.
    """

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        self.reasons: list[str] = []
        self.fixed = False

    def _text(self, index: int) -> str | None:
        value = self.fields[index]
        cleaned = " ".join(value.split())
        if cleaned != value:
            self.fixed = True
        if not cleaned:
            self.reasons.append(f"{COLUMNS[index]}: missing value")
            return None
        return cleaned

    def _reject(self, index: int, message: str) -> None:
        self.reasons.append(f"{COLUMNS[index]}: {message}")

    def integer(self, index: int, bounds: tuple[int, int]) -> int:
        text = self._text(index)
        if text is None:
            return 0
        if not _INTEGER.fullmatch(text):
            # A letter O typed for a zero ("4O"), only next to a real digit.
            zeroed = text.replace("O", "0").replace("o", "0")
            if not (any(c.isdigit() for c in text) and _INTEGER.fullmatch(zeroed)):
                self._reject(index, f"{text!r} is not an integer")
                return 0
            text = zeroed
            self.fixed = True
        value = int(text)
        low, high = bounds
        if not low <= value <= high:
            self._reject(index, f"{value} is outside {low}..{high}")
        return value

    def decimal(self, index: int, bounds: tuple[Decimal, Decimal]) -> Decimal:
        text = self._text(index)
        if text is None:
            return Decimal(0)
        if not _DECIMAL.fullmatch(text):
            self._reject(index, f"{text!r} is not a number")
            return Decimal(0)
        value = Decimal(text)
        rounded = value.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)
        if rounded != value:
            self.fixed = True
        low, high = bounds
        if not low <= rounded <= high:
            self._reject(index, f"{text} is outside {low}..{high}")
        return rounded

    def choice(self, index: int, allowed: tuple[str, ...], unknown: str | None = None) -> str:
        text = self._text(index)
        if text is None:
            return ""
        for option in allowed:
            if option.lower() == text.lower():
                if option != text:
                    self.fixed = True
                return option
        self._reject(index, f"{text!r} {unknown or 'is not one of ' + ', '.join(allowed)}")
        return ""

    def boolean(self, index: int) -> bool:
        text = self._text(index)
        if text is None:
            return False
        key = text.lower()
        if key not in BOOLEANS:
            self._reject(index, f"{text!r} is not a boolean (0 or 1)")
            return False
        if text not in ("0", "1"):
            self.fixed = True
        return BOOLEANS[key]
