"""Allowed values for the categorical columns, shared by the API and the loader."""

from typing import Literal, get_args

Gender = Literal["Male", "Female", "Other"]
SleepQuality = Literal["Poor", "Fair", "Good", "Excellent"]
StressLevel = Literal["Low", "Medium", "High"]
HealthIssues = Literal["None", "Mild", "Moderate", "Severe"]
Occupation = Literal["Office", "Healthcare", "Service", "Student", "Other"]
# A closed list: an unknown spelling is rejected instead of becoming a new filter value.
Country = Literal[
    "Australia",
    "Belgium",
    "Brazil",
    "Canada",
    "China",
    "Finland",
    "France",
    "Germany",
    "India",
    "Italy",
    "Japan",
    "Mexico",
    "Netherlands",
    "Norway",
    "South Korea",
    "Spain",
    "Sweden",
    "Switzerland",
    "UK",
    "USA",
]

GENDERS: tuple[str, ...] = get_args(Gender)
SLEEP_QUALITIES: tuple[str, ...] = get_args(SleepQuality)
STRESS_LEVELS: tuple[str, ...] = get_args(StressLevel)
HEALTH_ISSUES: tuple[str, ...] = get_args(HealthIssues)
OCCUPATIONS: tuple[str, ...] = get_args(Occupation)
COUNTRIES: tuple[str, ...] = get_args(Country)
