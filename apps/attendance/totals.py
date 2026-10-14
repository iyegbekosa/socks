"""Term attendance totals from the register, and the attendance line a report card shows.

A student's totals for a term count their records dated inside the term, whatever arm each
was taken in, so a student who moved arms keeps the days already recorded. Days on which no
register was taken for them do not count as opened.
"""

from dataclasses import dataclass
from typing import Literal, cast

from django.db.models import Count, Q

from apps.academics.models import Term
from apps.attendance.models import AttendanceRecord, AttendanceSettings
from apps.results.models import ClassResult, ReportComment, ResultSummary
from apps.students.models import Student

Status = AttendanceRecord.Status
Source = Literal["register", "manual"]


@dataclass(frozen=True)
class Totals:
    days_opened: int
    days_present: int
    times_late: int
    times_absent: int
    times_excused: int


@dataclass(frozen=True)
class CardAttendance:
    days_present: int | None
    days_opened: int | None
    times_late_shown: int | None
    source: Source | None


NO_ATTENDANCE = CardAttendance(None, None, None, None)


def term_totals(student_ids: list[int], term: Term) -> dict[int, Totals]:
    """Totals per student id, in one aggregate query. Students with no records in the term
    are absent from the dict.

    Totals are per student across arms (Ruling AB, accepted): a student who moved from an
    approved arm into one still in draft can gain records there, so a later regeneration of
    the old arm's card may differ. Published cards are stored files; regenerating is an
    explicit admin action."""
    if not student_ids:
        return {}
    rows = (
        AttendanceRecord.objects.filter(
            student_id__in=student_ids, date__gte=term.start_date, date__lte=term.end_date
        )
        .values("student_id")
        .annotate(
            opened=Count("date", distinct=True),
            present=Count("pk", filter=Q(status__in=[Status.PRESENT, Status.LATE])),
            late=Count("pk", filter=Q(status=Status.LATE)),
            absent=Count("pk", filter=Q(status=Status.ABSENT)),
            excused=Count("pk", filter=Q(status=Status.EXCUSED)),
        )
        .order_by()
    )
    return {
        r["student_id"]: Totals(r["opened"], r["present"], r["late"], r["absent"], r["excused"])
        for r in rows
    }


def card_attendance(
    totals: Totals | None, comment: ReportComment | None, late_threshold: int
) -> CardAttendance:
    """The register's totals when the student has records; otherwise the numbers typed on
    the comments page (P3) when both are set; otherwise nothing."""
    if totals is not None and totals.days_opened > 0:
        late = totals.times_late
        shown = late if late_threshold > 0 and late >= late_threshold else None
        return CardAttendance(totals.days_present, totals.days_opened, shown, "register")
    if comment is not None and comment.days_present is not None and comment.days_open is not None:
        return CardAttendance(comment.days_present, comment.days_open, None, "manual")
    return NO_ATTENDANCE


def late_threshold_for(arm_id: int | None, term: Term) -> int:
    """The late threshold for an arm's cards: the one snapshotted when its results were
    approved (Ruling AA), else the live setting."""
    if arm_id is not None:
        snap = (
            ClassResult.objects.filter(arm_id=arm_id, term=term)
            .values_list("late_threshold", flat=True)
            .first()
        )
        if snap is not None:
            return int(snap)
    return AttendanceSettings.current_late_threshold()


_LOAD = object()


def attendance_for_card(
    student: Student,
    term: Term,
    *,
    arm_id: int | None = None,
    comment: ReportComment | None | object = _LOAD,
) -> CardAttendance:
    """One card's attendance line (a single card, e.g. a preview). Batches use
    ``term_totals`` and ``card_attendance`` directly. ``arm_id``: the card's class (default:
    the student's results class for the term); ``comment``: pass it when already loaded."""
    if arm_id is None:
        arm_id = (
            ResultSummary.objects.filter(student=student, term=term)
            .values_list("arm_id", flat=True)
            .first()
        )
    loaded = (
        ReportComment.objects.filter(student=student, term=term).first()
        if comment is _LOAD
        else cast(ReportComment | None, comment)
    )
    return card_attendance(
        term_totals([student.pk], term).get(student.pk),
        loaded,
        late_threshold_for(arm_id, term),
    )
