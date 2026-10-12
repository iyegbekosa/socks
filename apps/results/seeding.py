from typing import Any

from django.db import transaction

from apps.academics.models import SECTIONS
from apps.results.models import AssessmentComponent, GradeBand

DEFAULT_COMPONENTS: list[tuple[str, int]] = [
    ("CA1", 10),
    ("CA2", 10),
    ("Assignment", 10),
    ("Test", 10),
    ("Exam", 60),
]

SSS_SCALE: list[tuple[int, int, str, str]] = [
    (75, 100, "A1", "Excellent"),
    (70, 74, "B2", "Very good"),
    (65, 69, "B3", "Good"),
    (60, 64, "C4", "Credit"),
    (55, 59, "C5", "Credit"),
    (50, 54, "C6", "Credit"),
    (45, 49, "D7", "Pass"),
    (40, 44, "E8", "Pass"),
    (0, 39, "F9", "Fail"),
]

BASIC_SCALE: list[tuple[int, int, str, str]] = [
    (70, 100, "A", "Excellent"),
    (60, 69, "B", "Very good"),
    (50, 59, "C", "Good"),
    (45, 49, "D", "Fair"),
    (40, 44, "E", "Poor"),
    (0, 39, "F", "Fail"),
]


def default_scale(section: str) -> list[tuple[int, int, str, str]]:
    return SSS_SCALE if section == "sss" else BASIC_SCALE


def seed_results(school: Any) -> None:
    """Create default components and grade bands for each offered section that has none.

    Each section is checked separately so a section added later still gets its defaults,
    while a section the school has customised (or emptied of components) is left alone.
    """
    offered = [s for s in SECTIONS if s in (school.levels_offered or [])]
    with transaction.atomic():
        for section in offered:
            if not AssessmentComponent.objects.filter(section=section).exists():
                for order, (name, mx) in enumerate(DEFAULT_COMPONENTS, start=1):
                    AssessmentComponent.objects.create(
                        section=section, name=name, max_score=mx, order=order
                    )
            if not GradeBand.objects.filter(section=section).exists():
                for lower, upper, grade, remark in default_scale(section):
                    GradeBand.objects.create(
                        section=section, lower=lower, upper=upper, grade=grade, remark=remark
                    )
