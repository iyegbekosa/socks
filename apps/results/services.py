from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.academics.models import SubjectOffering
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.results import scope
from apps.results.models import AssessmentComponent, ElectiveChoice, GradeBand, validate_scale
from apps.students.models import Enrolment, Student

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


def set_electives(
    offering: SubjectOffering, student_ids: list[int], *, by: Any, request: Any = None
) -> None:
    """Replace the set of students taking an elective. Any invalid id saves nothing."""
    if offering.is_compulsory:
        raise ValidationError("This subject is compulsory, so it has no elective choices.")
    wanted = {int(i) for i in student_ids}
    with transaction.atomic():
        school_lock(f"electives:{offering.pk}")
        valid = set(
            Enrolment.objects.filter(
                arm_id=offering.arm_id,
                session_id=offering.session_id,
                status=Enrolment.Status.ACTIVE,
                student__status=Student.Status.ACTIVE,
                student_id__in=wanted,
            ).values_list("student_id", flat=True)
        )
        if valid != wanted:
            raise ValidationError("Choose only active students in this class.")
        current = set(
            ElectiveChoice.objects.filter(offering=offering).values_list("student_id", flat=True)
        )
        stale = current - set(
            Enrolment.objects.filter(
                arm_id=offering.arm_id,
                session_id=offering.session_id,
                status=Enrolment.Status.ACTIVE,
                student__status=Student.Status.ACTIVE,
                student_id__in=current,
            ).values_list("student_id", flat=True)
        )
        added = sorted(wanted - current)
        removed = sorted((current - wanted) | stale)
        # Task 4: refuse removal when scores exist
        for sid in removed:
            if scope.has_student_scores(offering, Student.objects.get(pk=sid)):
                raise ValidationError("A student with scores for this subject cannot be removed.")
        ElectiveChoice.objects.filter(offering=offering, student_id__in=removed).delete()
        ElectiveChoice.objects.bulk_create(
            [ElectiveChoice(offering=offering, student_id=sid) for sid in added]
        )
        if added or removed:
            record(
                "results.elective_changed",
                request=request,
                actor=by,
                after={"offering_id": offering.pk, "added": added, "removed": removed},
            )
