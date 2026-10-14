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
from collections.abc import Callable
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Min
from django.db.models.functions import Now
from django.utils import timezone

from apps.academics.models import ClassArm, FormTeacher, SubjectOffering, Term
from apps.accounts.roles import PRINCIPAL, SCHOOL_ADMIN
from apps.attendance.models import AttendanceSettings
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.core.logging import log_exception_redacted
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
    _start_cards(arm.pk, term.pk)
    _start_notify(arm.pk, term.pk)


def on_class_unlocked(arm: ClassArm, term: Term) -> None:
    """Runs on commit after a class is unlocked: removes its report cards (files on commit)."""
    from apps.results.cards import remove_report_cards

    remove_report_cards(arm, term)


# --- queueing background work --------------------------------------------------------------
# These run on commit, after the state change is saved. A broker or queue failure must not
# turn the request into a 500: it is logged (type only) and shown on the class page, where
# "Retry compute" / "Regenerate report cards" start the work again.


def _start(what: str, arm_id: int, term_id: int, enqueue: Callable[[], Any], **error: str) -> bool:
    try:
        enqueue()
    except Exception as exc:
        logger.error(
            "Could not queue %s for class %s term %s (%s).",
            what,
            arm_id,
            term_id,
            type(exc).__name__,
        )
        ClassResult.objects.filter(arm_id=arm_id, term_id=term_id).update(**error)
        return False
    return True


def _start_compute(arm_id: int, term_id: int) -> bool:
    from apps.results.tasks import compute_class_task

    return _start(
        "computation",
        arm_id,
        term_id,
        lambda: compute_class_task.apply_async(args=[arm_id, term_id], queue="default"),
        compute_error=ClassResult.START_ERROR,
    )


def _start_cards(arm_id: int, term_id: int) -> bool:
    from apps.results.tasks import generate_report_cards_task

    return _start(
        "report cards",
        arm_id,
        term_id,
        lambda: generate_report_cards_task.apply_async(args=[arm_id, term_id], queue="reports"),
        cards_error=ClassResult.START_ERROR,
    )


def _start_notify(arm_id: int, term_id: int) -> bool:
    from apps.results.tasks import notify_published_task

    return _start(
        "notifications",
        arm_id,
        term_id,
        lambda: notify_published_task.apply_async(args=[arm_id, term_id], queue="notifications"),
        cards_error=ClassResult.NOTIFY_START_ERROR,
    )


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


def needs_compute(klass: ClassResult | None) -> bool:
    """An approved class whose computation failed, never started, or predates its approval."""
    if klass is None or klass.status != ClassResult.Status.APPROVED:
        return False
    if klass.compute_error:
        return True
    first = ResultSummary.objects.filter(arm_id=klass.arm_id, term_id=klass.term_id).aggregate(
        first=Min("computed_at")
    )["first"]
    return first is None or klass.approved_at is None or first < klass.approved_at


def can_manage_class(user: Any) -> bool:
    """Principal or school admin: publish, retry compute, regenerate report cards."""
    return _has_role(user, PRINCIPAL, SCHOOL_ADMIN)


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
        # Freeze the card's "Late n times" rule with the class (Ruling AA).
        klass.late_threshold = AttendanceSettings.current_late_threshold()
        klass.save(
            update_fields=[
                "status",
                "approved_at",
                "approved_by",
                "compute_error",
                "late_threshold",
            ]
        )
        klass.refresh_from_db(fields=["approved_at"])
        record(
            "results.class_approved", request=request, actor=by, obj=klass, after=_ids(arm, term)
        )
        arm_id, term_id = arm.pk, term.pk
        transaction.on_commit(lambda: _start_compute(arm_id, term_id))
    return klass


def run_compute(arm: ClassArm, term: Term) -> None:
    """Compute an approved class. A data problem (e.g. a blank cell) returns it to
    ``reviewed`` with ``compute_error`` set to the reason. Any other failure keeps it
    ``approved`` with a generic ``compute_error`` ("Retry compute" on the class page). A class
    no longer approved is left alone."""
    try:
        _run_compute(arm, term)
    except Exception:
        log_exception_redacted(logger, "Computing class %s term %s failed", arm.pk, term.pk)
        with transaction.atomic():
            school_lock(f"results:{arm.pk}:{term.pk}")
            ClassResult.objects.filter(
                arm=arm, term=term, status=ClassResult.Status.APPROVED
            ).update(compute_error=ClassResult.COMPUTE_FAILED)


def _run_compute(arm: ClassArm, term: Term) -> None:
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = ClassResult.objects.select_for_update().filter(arm=arm, term=term).first()
        if klass is None or klass.status != ClassResult.Status.APPROVED:
            return
        try:
            compute_class(arm, term)  # its own savepoint: a failure leaves nothing half-written
        except ValidationError as exc:
            reason = " ".join(exc.messages)[:300]
            logger.warning("Computing class %s term %s failed: %s", arm.pk, term.pk, reason)
            klass.status = ClassResult.Status.REVIEWED
            klass.compute_error = reason
            klass.save(update_fields=["status", "compute_error"])
        except Exception:
            log_exception_redacted(logger, "Computing class %s term %s failed", arm.pk, term.pk)
            klass.compute_error = ClassResult.COMPUTE_FAILED
            klass.save(update_fields=["compute_error"])
        else:
            if klass.compute_error:
                klass.compute_error = ""
                klass.save(update_fields=["compute_error"])


def retry_compute(arm: ClassArm, term: Term, *, by: Any, request: Any = None) -> ClassResult:
    """Queue the computation of an approved class again, by the principal or school admin,
    when it failed, never started, or is older than the approval."""
    if not can_manage_class(by):
        raise PermissionDenied
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = _locked_class(arm, term)
        if klass.status != ClassResult.Status.APPROVED:
            raise ResultsStateError("Only an approved class can be computed again.")
        if not needs_compute(klass):
            raise ResultsStateError("This class's results are already computed.")
        klass.compute_error = ""
        klass.save(update_fields=["compute_error"])
        record(
            "results.compute_retried", request=request, actor=by, obj=klass, after=_ids(arm, term)
        )
        arm_id, term_id = arm.pk, term.pk
        transaction.on_commit(lambda: _start_compute(arm_id, term_id))
    return klass


def regenerate_report_cards(
    arm: ClassArm, term: Term, *, by: Any, request: Any = None
) -> ClassResult:
    """Queue a published class's report cards again, by the principal or school admin; also
    queues the guardian notifications when those could not start at publish."""
    if not can_manage_class(by):
        raise PermissionDenied
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = _locked_class(arm, term)
        if klass.status != ClassResult.Status.PUBLISHED:
            raise ResultsStateError("Only a published class's report cards can be regenerated.")
        renotify = klass.cards_error == ClassResult.NOTIFY_START_ERROR
        klass.cards_error = ""
        klass.save(update_fields=["cards_error"])
        record(
            "results.report_cards_regenerated",
            request=request,
            actor=by,
            obj=klass,
            after={**_ids(arm, term), "notify": renotify},
        )
        arm_id, term_id = arm.pk, term.pk

        def start() -> None:
            _start_cards(arm_id, term_id)
            if renotify:
                _start_notify(arm_id, term_id)

        transaction.on_commit(start)
    return klass


def publish_class(arm: ClassArm, term: Term, *, by: Any, request: Any = None) -> ClassResult:
    """approved → published, by the principal or school admin, once results are computed."""
    if not can_manage_class(by):
        raise PermissionDenied
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = _locked_class(arm, term)
        if klass.status != ClassResult.Status.APPROVED:
            raise ResultsStateError("Only an approved class can be published.")
        if klass.compute_error:
            raise ValidationError("Computing the results failed. Retry the computation first.")
        if needs_compute(klass):
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
        klass.late_threshold = None  # re-snapshotted at the next approval
        klass.save(update_fields=["status", "compute_error", "late_threshold"])
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
