from typing import Any

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
    """Create default levels and subjects for the school's sections (idempotent, no arms)."""
    offered = [s for s in SECTIONS if s in (school.levels_offered or [])]
    for section in offered:
        for order, name in enumerate(LEVELS[section], start=1):
            ClassLevel.objects.get_or_create(
                name=name,
                defaults={"section": section, "order": SECTIONS.index(section) * 10 + order},
            )
        for name in SUBJECTS[section]:
            subject = Subject.objects.filter(name=name).first()
            if subject is None:
                Subject.objects.create(name=name, code=slugify(name), sections=[section])
            elif section not in subject.sections:
                subject.sections = [s for s in SECTIONS if s in {*subject.sections, section}]
                subject.save(update_fields=["sections"])
