from typing import Any

from django.db import transaction
from django.utils.text import slugify

from apps.academics.models import SECTIONS, ClassLevel, Subject

LEVELS: dict[str, list[str]] = {
    "nursery": ["Nursery 1", "Nursery 2"],
    "primary": [f"Primary {i}" for i in range(1, 7)],
    "jss": [f"JSS {i}" for i in range(1, 4)],
    "sss": [f"SSS {i}" for i in range(1, 4)],
}

SUBJECTS: dict[str, list[str]] = {
    "nursery": [
        "Numeracy",
        "Literacy",
        "Basic Science",
        "Social Habits",
        "Creative Arts",
        "Rhymes",
    ],
    "primary": [
        "English Language",
        "Mathematics",
        "Basic Science and Technology",
        "Social Studies",
        "Civic Education",
        "Christian Religious Studies",
        "Islamic Religious Studies",
        "Cultural and Creative Arts",
        "Physical and Health Education",
        "Nigerian Language",
        "Computer Studies",
    ],
    "jss": [
        "English Language",
        "Mathematics",
        "Basic Science",
        "Basic Technology",
        "Social Studies",
        "Civic Education",
        "Business Studies",
        "Computer Studies",
        "Christian Religious Studies",
        "Islamic Religious Studies",
        "Cultural and Creative Arts",
        "Physical and Health Education",
        "French",
        "Nigerian Language",
        "Agricultural Science",
        "Home Economics",
    ],
    "sss": [
        "English Language",
        "Mathematics",
        "Civic Education",
        "Biology",
        "Physics",
        "Chemistry",
        "Further Mathematics",
        "Agricultural Science",
        "Economics",
        "Government",
        "Literature in English",
        "Geography",
        "Christian Religious Studies",
        "Islamic Religious Studies",
        "Financial Accounting",
        "Commerce",
        "Computer Studies",
        "Data Processing",
    ],
}


def seed_academics(school: Any) -> None:
    """Create starting levels and subjects (no arms).

    Defaults are a one-time starting point: levels are created only when the school has
    none, subjects only when the school has none, so renames and deletions are respected.
    """
    offered = [s for s in SECTIONS if s in (school.levels_offered or [])]
    with transaction.atomic():
        if not ClassLevel.objects.exists():
            for section in offered:
                for order, name in enumerate(LEVELS[section], start=1):
                    ClassLevel.objects.create(
                        name=name, section=section, order=SECTIONS.index(section) * 10 + order
                    )
        if not Subject.objects.exists():
            by_name: dict[str, set[str]] = {}
            for section in offered:
                for name in SUBJECTS[section]:
                    by_name.setdefault(name, set()).add(section)
            for name, found in by_name.items():
                Subject.objects.create(
                    name=name,
                    code=slugify(name),
                    sections=[s for s in SECTIONS if s in found],
                )
