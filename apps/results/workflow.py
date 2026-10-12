"""Results workflow: submit/recall sheets, review/approve/publish/unlock a class (FR-08).

Locks (always in this order when more than one is needed):

1. ``scores:{offering}:{term}`` for each sheet that changes, sorted by offering pk;
2. ``results:{arm}:{term}``.

Every transition re-reads and checks the state inside its locks. A wrong state raises
``ResultsStateError`` (409 at the API); other unmet conditions raise ``ValidationError``; a
caller who may not act raises ``PermissionDenied``. Messages and audit rows carry ids and
counts only.
"""

import logging
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Min
from django.db.models.functions import Now
from django.utils import timezone

from apps.academics.models import ClassArm, FormTeacher, SubjectOffering, Term
from apps.accounts.roles import PRINCIPAL, SCHOOL_ADMIN
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.results.compute import compute_class
from apps.results.models import (
    ClassResult,
    ReportComment,
    ResultSummary,
    Score,
    ScoreSheet,
    SubjectResult,
    components_complete,
    components_for,
)
from apps.results.scope import (
    can_enter_scores,
    ranked_students,
    required_offerings,
    students_taking,
)

logger = logging.getLogger(__name__)

MAX_REASON = 300
CLASS_MOVED_ON = "This class's results have moved on, so its score sheets can no longer change."
TERM_MESSAGE = "This term is not in the subject's session."


class ResultsStateError(ValidationError):
    """The sheet or class is not in the state this action needs."""


# --- extension points ----------------------------------------------------------------------


def on_class_published(arm: ClassArm, term: Term) -> None:
    """Runs on commit after a class is published: queues its report cards and the guardian
    notifications."""
    from apps.results.tasks import generate_report_cards_task, notify_published_task

    generate_report_cards_task.apply_async(args=[arm.pk, term.pk], queue="reports")
    notify_published_task.apply_async(args=[arm.pk, term.pk], queue="notifications")


def on_class_unlocked(arm: ClassArm, term: Term) -> None:
    """Runs on commit after a class is unlocked: removes its report cards (files on commit)."""
    from apps.results.cards import remove_report_cards

    remove_report_cards(arm, term)


# --- helpers -------------------------------------------------------------------------------


def _ids(arm: ClassArm, term: Term) -> dict[str, int]:
    return {"arm_id": arm.pk, "term_id": term.pk}


def _locked_class(arm: ClassArm, term: Term) -> ClassResult:
    ClassResult.objects.get_or_create(arm=arm, term=term)
    return ClassResult.objects.select_for_update().get(arm=arm, term=term)


def _locked_sheet(offering: SubjectOffering, term: Term) -> ScoreSheet:
    ScoreSheet.objects.get_or_create(offering=offering, term=term)
    return ScoreSheet.objects.select_for_update().get(offering=offering, term=term)


def _check_term(offering: SubjectOffering, term: Term) -> None:
    if term.session_id != offering.session_id:
        raise ValidationError(TERM_MESSAGE)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _is_verified(user: Any) -> bool:
    check = getattr(user, "is_verified", None)  # set on request.user by OTPMiddleware
    return bool(check and check())


def is_form_teacher(user: Any, arm: ClassArm, term: Term) -> bool:
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if getattr(user, "kind", None) != "staff":
        return False
    return FormTeacher.objects.filter(arm=arm, session_id=term.session_id, teacher=user).exists()


def _has_role(user: Any, *roles: str) -> bool:
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return getattr(user, "kind", None) == "staff" and bool(user.has_role(*roles))


def blank_cells(offering: SubjectOffering, term: Term) -> int:
    """Blank cells across every ranked taker and every component of the section."""
    comps = components_for(offering.arm.level.section)
    takers = list(students_taking(offering).order_by().values_list("pk", flat=True))
    filled = Score.objects.filter(
        offering=offering,
        term=term,
        student_id__in=takers,
        component__in=comps,
        value__isnull=False,
    ).count()
    return len(takers) * len(comps) - filled


# --- score sheets --------------------------------------------------------------------------


def submit_sheet(
    offering: SubjectOffering, term: Term, *, by: Any, request: Any = None
) -> ScoreSheet:
    """draft → submitted, by the assigned teacher, once every taker's cells are filled."""
    if not can_enter_scores(by, offering):
        raise PermissionDenied
    _check_term(offering, term)
    with transaction.atomic():
        school_lock(f"scores:{offering.pk}:{term.pk}")
        school_lock(f"results:{offering.arm_id}:{term.pk}")
        sheet = _locked_sheet(offering, term)
        klass = _locked_class(offering.arm, term)
        if klass.status != ClassResult.Status.DRAFT:
            raise ResultsStateError(CLASS_MOVED_ON)
        if sheet.status != ScoreSheet.Status.DRAFT:
            raise ResultsStateError("This score sheet is already submitted.")
        if not components_complete(offering.arm.level.section):
            raise ValidationError(
                "The assessment components for this section must total 100 before submitting."
            )
        blanks = blank_cells(offering, term)
        if blanks:
            raise ValidationError(
                f"{blanks} score {_plural(blanks, 'cell is', 'cells are')} blank. "
                "Fill every cell (enter 0 for an absent student) before submitting."
            )
        now = timezone.now()
        sheet.status = ScoreSheet.Status.SUBMITTED
        sheet.submitted_by = by
        sheet.submitted_at = now
        if sheet.first_submitted_at is None:
            sheet.first_submitted_at = now
        sheet.save(update_fields=["status", "submitted_by", "submitted_at", "first_submitted_at"])
        record(
            "results.sheet_submitted",
            request=request,
            actor=by,
            obj=sheet,
            after={"offering_id": offering.pk, "term_id": term.pk, "sheet_id": sheet.pk},
        )
    return sheet


def recall_sheet(
    offering: SubjectOffering, term: Term, *, by: Any, request: Any = None
) -> ScoreSheet:
    """submitted → draft, by the assigned teacher, only while the class is draft."""
    if not can_enter_scores(by, offering):
        raise PermissionDenied
    _check_term(offering, term)
    with transaction.atomic():
        school_lock(f"scores:{offering.pk}:{term.pk}")
        school_lock(f"results:{offering.arm_id}:{term.pk}")
        sheet = _locked_sheet(offering, term)
        klass = _locked_class(offering.arm, term)
        if klass.status != ClassResult.Status.DRAFT:
            raise ResultsStateError(CLASS_MOVED_ON)
        if sheet.status != ScoreSheet.Status.SUBMITTED:
            raise ResultsStateError("This score sheet is not submitted.")
        sheet.status = ScoreSheet.Status.DRAFT
        sheet.save(update_fields=["status"])
        record(
            "results.sheet_recalled",
            request=request,
            actor=by,
            obj=sheet,
            after={"offering_id": offering.pk, "term_id": term.pk, "sheet_id": sheet.pk},
        )
    return sheet


# --- class results -------------------------------------------------------------------------


def review_class(arm: ClassArm, term: Term, *, by: Any, request: Any = None) -> ClassResult:
    """draft → reviewed, by the form teacher: every required sheet is submitted and every
    ranked student has a class-teacher comment."""
    if not is_form_teacher(by, arm, term):
        raise PermissionDenied
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = _locked_class(arm, term)
        if klass.status != ClassResult.Status.DRAFT:
            raise ResultsStateError("This class has already been reviewed.")
        required = list(required_offerings(arm, term).values_list("pk", flat=True))
        if not required:
            raise ValidationError("This class has no subjects with students this session.")
        submitted = ScoreSheet.objects.filter(
            offering_id__in=required, term=term, status=ScoreSheet.Status.SUBMITTED
        ).count()
        pending = len(required) - submitted
        if pending:
            raise ValidationError(
                f"{pending} of {len(required)} score {_plural(len(required), 'sheet', 'sheets')} "
                f"{_plural(pending, 'is', 'are')} not submitted yet."
            )
        students = list(ranked_students(arm, term).order_by().values_list("pk", flat=True))
        commented = (
            ReportComment.objects.filter(term=term, student_id__in=students)
            .exclude(class_teacher_comment__regex=r"^\s*$")
            .count()
        )
        missing = len(students) - commented
        if missing:
            raise ValidationError(
                f"{missing} {_plural(missing, 'student has', 'students have')} "
                "no class teacher comment yet."
            )
        klass.status = ClassResult.Status.REVIEWED
        klass.reviewed_at = timezone.now()
        klass.reviewed_by = by
        klass.save(update_fields=["status", "reviewed_at", "reviewed_by"])
        record(
            "results.class_reviewed", request=request, actor=by, obj=klass, after=_ids(arm, term)
        )
    return klass


def approve_class(arm: ClassArm, term: Term, *, by: Any, request: Any = None) -> ClassResult:
    """reviewed → approved, by the principal; computation runs on commit."""
    if not _has_role(by, PRINCIPAL):
        raise PermissionDenied
    from apps.results.tasks import compute_class_task

    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = _locked_class(arm, term)
        if klass.status != ClassResult.Status.REVIEWED:
            raise ResultsStateError("Only a reviewed class can be approved.")
        klass.status = ClassResult.Status.APPROVED
        # Database time, like ResultSummary.computed_at: publish compares the two, and the
        # web and worker hosts' clocks may differ.
        klass.approved_at = Now()
        klass.approved_by = by
        klass.compute_error = ""
        klass.save(update_fields=["status", "approved_at", "approved_by", "compute_error"])
        klass.refresh_from_db(fields=["approved_at"])
        record(
            "results.class_approved", request=request, actor=by, obj=klass, after=_ids(arm, term)
        )
        arm_id, term_id = arm.pk, term.pk
        transaction.on_commit(
            lambda: compute_class_task.apply_async(args=[arm_id, term_id], queue="default")
        )
    return klass


def run_compute(arm: ClassArm, term: Term) -> None:
    """Compute an approved class. A failure (e.g. a blank cell) returns it to ``reviewed`` with
    ``compute_error`` set. A class no longer approved is left alone."""
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = ClassResult.objects.select_for_update().filter(arm=arm, term=term).first()
        if klass is None or klass.status != ClassResult.Status.APPROVED:
            return
        try:
            compute_class(arm, term)
        except ValidationError as exc:
            reason = " ".join(exc.messages)[:300]
            logger.warning("Computing class %s term %s failed: %s", arm.pk, term.pk, reason)
            klass.status = ClassResult.Status.REVIEWED
            klass.compute_error = reason
            klass.save(update_fields=["status", "compute_error"])


def publish_class(arm: ClassArm, term: Term, *, by: Any, request: Any = None) -> ClassResult:
    """approved → published, by the principal or school admin, once results are computed."""
    if not _has_role(by, PRINCIPAL, SCHOOL_ADMIN):
        raise PermissionDenied
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = _locked_class(arm, term)
        if klass.status != ClassResult.Status.APPROVED:
            raise ResultsStateError("Only an approved class can be published.")
        if klass.compute_error:
            raise ValidationError("Computing the results failed; unlock and fix the class first.")
        computed = ResultSummary.objects.filter(arm=arm, term=term).aggregate(
            first=Min("computed_at")
        )["first"]
        if computed is None or klass.approved_at is None or computed < klass.approved_at:
            raise ValidationError("The results have not been computed yet. Try again in a moment.")
        klass.status = ClassResult.Status.PUBLISHED
        klass.published_at = timezone.now()
        klass.published_by = by
        klass.save(update_fields=["status", "published_at", "published_by"])
        record(
            "results.class_published", request=request, actor=by, obj=klass, after=_ids(arm, term)
        )
        transaction.on_commit(lambda: on_class_published(arm, term))
    return klass


def unlock_class(
    arm: ClassArm,
    term: Term,
    *,
    sheets: list[ScoreSheet],
    reason: str,
    by: Any,
    request: Any = None,
) -> ClassResult:
    """Return the class and the chosen sheets to draft, by a verified school admin.

    Removes the class's computed results for the term so stale results cannot be published.
    """
    if not _has_role(by, SCHOOL_ADMIN) or not _is_verified(by):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for unlocking.")
    if len(reason) > MAX_REASON:
        raise ValidationError(f"Keep the reason to {MAX_REASON} characters or fewer.")
    chosen = sorted({s.pk: s for s in sheets}.values(), key=lambda s: (s.offering_id, s.pk))
    with transaction.atomic():
        for s in chosen:
            school_lock(f"scores:{s.offering_id}:{term.pk}")
        school_lock(f"results:{arm.pk}:{term.pk}")
        locked = list(
            ScoreSheet.objects.select_for_update()
            .filter(pk__in=[s.pk for s in chosen])
            .filter(term=term, offering__arm=arm)
            .order_by("offering_id", "pk")
        )
        if len(locked) != len(chosen):
            raise ValidationError("Choose score sheets from this class and term only.")
        klass = _locked_class(arm, term)
        if klass.status == ClassResult.Status.DRAFT and not any(
            s.status != ScoreSheet.Status.DRAFT for s in locked
        ):
            raise ResultsStateError("This class is already open for changes.")
        klass.status = ClassResult.Status.DRAFT
        klass.compute_error = ""
        klass.save(update_fields=["status", "compute_error"])
        ScoreSheet.objects.filter(pk__in=[s.pk for s in locked]).update(
            status=ScoreSheet.Status.DRAFT
        )
        SubjectResult.objects.filter(
            term=term, offering__arm=arm, offering__session_id=term.session_id
        ).delete()
        ResultSummary.objects.filter(term=term, arm=arm).delete()
        record(
            "results.unlocked",
            request=request,
            actor=by,
            obj=klass,
            after={**_ids(arm, term), "sheet_ids": [s.pk for s in locked], "reason": reason},
        )
        transaction.on_commit(lambda: on_class_unlocked(arm, term))
    return klass
