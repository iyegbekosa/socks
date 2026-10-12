from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.academics.models import SubjectOffering
from apps.academics.services import current_term
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.results import scope
from apps.results.models import (
    AssessmentComponent,
    ClassResult,
    ElectiveChoice,
    GradeBand,
    Score,
    validate_scale,
)
from apps.students.models import Enrolment, Student

ELECTIVES_LOCKED = (
    "This class's results have moved past draft, so its elective choices can no longer "
    "change. A school admin can unlock the class first."
)
LOCKED_MESSAGE = (
    "Scores have already been entered for this section, so its assessment components "
    "can no longer be changed."
)
REMOVED_IN_USE_MESSAGE = (
    "A component you removed has scores from an earlier session, so it cannot be removed."
)


def save_components(section: str, rows: list[dict], *, by: Any, request: Any = None) -> None:
    names = [r["name"].strip() for r in rows]
    if not rows:
        raise ValidationError("Add at least one assessment component.")
    if len({n.lower() for n in names}) != len(names):
        raise ValidationError("Component names must be different from each other.")
    with transaction.atomic():
        school_lock("results:settings")
        # Lock the rows first: save_cells locks them too, so no score lands after this check.
        existing = {
            c.name.lower(): c
            for c in AssessmentComponent.objects.select_for_update().filter(section=section)
        }
        if scope.has_scores(section):
            raise ValidationError(LOCKED_MESSAGE)
        # Update in place by name (case-insensitive) so component pks stay stable for scores.
        wanted = {r["name"].strip().lower() for r in rows}
        removed = [c for key, c in existing.items() if key not in wanted]
        if Score.objects.filter(component__in=removed, value__isnull=False).exists():
            raise ValidationError(REMOVED_IN_USE_MESSAGE)
        Score.objects.filter(component__in=removed).delete()  # blank cells only
        AssessmentComponent.objects.filter(pk__in=[c.pk for c in removed]).delete()
        for row in rows:
            name = row["name"].strip()
            comp = existing.get(name.lower())
            if comp is None:
                AssessmentComponent.objects.create(
                    section=section, name=name, max_score=row["max_score"], order=row["order"]
                )
            else:
                comp.name = name
                comp.max_score = row["max_score"]
                comp.order = row["order"]
                comp.save(update_fields=["name", "max_score", "order"])
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
    from apps.results.workflow import ResultsStateError

    with transaction.atomic():
        school_lock(f"electives:{offering.pk}")
        # Who takes a subject decides which cells must be filled: once the class has moved
        # past draft for the current term, a new taker would leave a submitted sheet blank.
        term = current_term()
        if term is not None and term.session_id == offering.session_id:
            school_lock(f"results:{offering.arm_id}:{term.pk}")
            status = (
                ClassResult.objects.filter(arm_id=offering.arm_id, term=term)
                .values_list("status", flat=True)
                .first()
            )
            if status not in (None, ClassResult.Status.DRAFT):
                raise ResultsStateError(ELECTIVES_LOCKED)
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
        # Students still in the class keep the subject once they have scores. Stale choices
        # (moved or withdrawn students) always go; their scores stay as history.
        still_enrolled = (current - wanted) - stale
        with_scores = (
            Score.objects.filter(
                offering=offering, student_id__in=still_enrolled, value__isnull=False
            )
            .values("student_id")
            .distinct()
            .count()
        )
        if with_scores:
            noun = "student already has" if with_scores == 1 else "students already have"
            raise ValidationError(
                f"{with_scores} {noun} scores for this subject and cannot be removed."
            )
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
