from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.services import record
from apps.core.locks import school_lock
from apps.results import scope
from apps.results.models import AssessmentComponent, GradeBand, validate_scale

LOCKED_MESSAGE = (
    "Scores have already been entered for this section, so its assessment components "
    "can no longer be changed."
)


def save_components(section: str, rows: list[dict], *, by: Any, request: Any = None) -> None:
    names = [r["name"].strip() for r in rows]
    if not rows:
        raise ValidationError("Add at least one assessment component.")
    if len({n.lower() for n in names}) != len(names):
        raise ValidationError("Component names must be different from each other.")
    with transaction.atomic():
        school_lock("results:settings")
        if scope.has_scores(section):
            raise ValidationError(LOCKED_MESSAGE)
        AssessmentComponent.objects.filter(section=section).delete()
        for row in rows:
            AssessmentComponent.objects.create(
                section=section,
                name=row["name"].strip(),
                max_score=row["max_score"],
                order=row["order"],
            )
        items = list(
            AssessmentComponent.objects.filter(section=section).values_list("name", "max_score")
        )
        record(
            "results.components_changed",
            request=request,
            actor=by,
            after={"section": section, "items": [[n, m] for n, m in items]},
        )


def save_scale(section: str, rows: list[dict], *, by: Any, request: Any = None) -> None:
    bands = [
        GradeBand(
            section=section,
            lower=r["lower"],
            upper=r["upper"],
            grade=r["grade"].strip(),
            remark=r.get("remark", "").strip(),
        )
        for r in rows
    ]
    validate_scale(bands)
    with transaction.atomic():
        school_lock("results:settings")
        GradeBand.objects.filter(section=section).delete()
        GradeBand.objects.bulk_create(bands)
        record(
            "results.scale_changed",
            request=request,
            actor=by,
            after={
                "section": section,
                "items": [
                    [b.lower, b.upper, b.grade] for b in sorted(bands, key=lambda b: -b.lower)
                ],
            },
        )
