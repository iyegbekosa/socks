"""Taking and editing a class arm's daily register."""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F, FilteredRelation, Q, QuerySet
from django.utils import timezone

from apps.academics.models import ClassArm, FormTeacher, Term
from apps.academics.services import current_term
from apps.accounts.roles import PRINCIPAL, SCHOOL_ADMIN, TEACHER
from apps.attendance import hooks
from apps.attendance.days import RegisterDateError, check_register_date
from apps.attendance.models import AttendanceRecord, AttendanceSettings
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.results.models import ClassResult
from apps.students.models import Enrolment, Student

__all__ = [
    "LOCKED_MESSAGE",
    "STATUSES",
    "RegisterDateError",
    "RegisterLocked",
    "Row",
    "SaveResult",
    "access",
    "can_edit",
    "can_take",
    "editable_by",
    "is_locked",
    "parse_iso_date",
    "rows_for",
    "save_register",
    "students_for",
]

STATUSES: tuple[str, ...] = tuple(AttendanceRecord.Status.values)
ABSENT = AttendanceRecord.Status.ABSENT
LABELS: dict[str, str] = dict(AttendanceRecord.Status.choices)

# Access levels. The principal and school_admin may take any arm's register and edit any date
# in the current term; the arm's form teacher for the session only within the edit window.
FULL = "full"
FORM_TEACHER = "form_teacher"

LOCKED_MESSAGE = "This register is locked because the class's results have left draft."
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class RegisterLocked(Exception):
    message = LOCKED_MESSAGE


@dataclass
class SaveResult:
    created: int = 0
    # {student_id: (status before, status after)}; before is None for a new record.
    changed: dict[int, tuple[str | None, str]] = field(default_factory=dict)
    newly_absent: list[int] = field(default_factory=list)  # record ids


@dataclass
class Row:
    student: Student
    status: str | None  # None: no record yet
    # The arm this date's record was taken in, when it is not this register's arm (the student
    # moved): the row is read-only here and never overwritten from here.
    elsewhere: ClassArm | None = None

    @property
    def label(self) -> str:
        return LABELS.get(self.status or "", "Not recorded")


def parse_iso_date(raw: str) -> date | None:
    """A ``YYYY-MM-DD`` date (exactly that form), else None."""
    if not isinstance(raw, str) or not ISO_DATE.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _listed(arm: ClassArm, term: Term) -> QuerySet[Student]:
    """Active students with an active enrolment in ``arm`` for ``term``'s session.

    Enrolments have no dates (one per student per session), so this is the current enrolment.
    """
    return (
        Student.objects.filter(
            status=Student.Status.ACTIVE,
            enrolments__arm=arm,
            enrolments__session_id=term.session_id,
            enrolments__status=Enrolment.Status.ACTIVE,
        )
        .select_related("user")
        .order_by("last_name", "first_name", "pk")
    )


def students_for(arm: ClassArm, d: date, *, term: Term | None = None) -> list[Student]:
    """The students on ``arm``'s register for ``d``, by surname, then first name."""
    if term is None:
        term = Term.objects.filter(start_date__lte=d, end_date__gte=d).first()
    return [] if term is None else list(_listed(arm, term))


@dataclass
class _Listed:
    """A listed student and their record for the date (all None without one)."""

    student: Student
    rec_id: int | None
    rec_status: str | None
    rec_arm_id: int | None

    @property
    def pk(self) -> int:
        return int(self.student.pk)


def _listed_with_records(arm: ClassArm, d: date, term: Term) -> list[_Listed]:
    """``students_for`` with each student's record for ``d``, in one query (a record is
    unique per student and date)."""
    qs = _listed(arm, term).annotate(
        day=FilteredRelation("attendance", condition=Q(attendance__date=d)),
        rec_id=F("day__id"),
        rec_status=F("day__status"),
        rec_arm_id=F("day__arm_id"),
    )
    # The annotations are not declared on Student, so read them from the instance dict.
    return [_Listed(s, vars(s)["rec_id"], vars(s)["rec_status"], vars(s)["rec_arm_id"]) for s in qs]


def access(user: Any, arm: ClassArm, term: Term | None) -> str | None:
    """``FULL``, ``FORM_TEACHER`` or None (may not take this arm's register).

    Like ``results.scope.can_open_comments``: active staff only; the form teacher must still
    hold the teacher role (a removed role does not delete their ``FormTeacher`` rows).
    """
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    if getattr(user, "kind", None) != "staff":
        return None
    roles = user.roles
    if roles & {PRINCIPAL, SCHOOL_ADMIN}:
        return FULL
    if TEACHER not in roles or term is None:
        return None
    if FormTeacher.objects.filter(arm=arm, session_id=term.session_id, teacher=user).exists():
        return FORM_TEACHER
    return None


def can_take(user: Any, arm: ClassArm, term: Term | None) -> bool:
    return access(user, arm, term) is not None


def _edit_window_days() -> int:
    value = (
        AttendanceSettings.objects.filter(pk=1).values_list("edit_window_days", flat=True).first()
    )
    if value is None:
        return int(AttendanceSettings._meta.get_field("edit_window_days").default)
    return int(value)


def editable_by(level: str | None, d: date, term: Term | None) -> bool:
    """Whether a user with access ``level`` may save ``d``'s register (results lock aside).

    ``d`` must be in ``term``, the current term. For the form teacher, a first save on a date
    outside the edit window counts as an edit, so it is refused too.
    """
    if level is None or term is None or not term.is_current:
        return False
    if not (term.start_date <= d <= term.end_date):
        return False
    if level == FULL:
        return True
    return (timezone.localdate() - d).days <= _edit_window_days()


def can_edit(user: Any, arm: ClassArm, d: date, *, term: Term | None = None) -> bool:
    if term is None:
        term = current_term()
    return editable_by(access(user, arm, term), d, term)


def is_locked(arm: ClassArm, term: Term) -> bool:
    """True once the arm's results for the term have left draft (a results unlock re-opens)."""
    return (
        ClassResult.objects.filter(arm=arm, term=term)
        .exclude(status=ClassResult.Status.DRAFT)
        .exists()
    )


def _locked_for_save(arm: ClassArm, term: Term) -> bool:
    """``is_locked``, reading the class row ``FOR SHARE``: a results transition (which takes
    the row ``FOR UPDATE``) waits for this save to commit, so no record changes as the class
    moves on. Django has no FOR SHARE option, hence raw.

    With no class row yet there is nothing to lock; a row is created once any score is saved,
    long before review, so a results transition cannot race a save in practice."""
    table = ClassResult._meta.db_table
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT status FROM {table} WHERE arm_id = %s AND term_id = %s FOR SHARE",
            [arm.pk, term.pk],
        )
        row = cur.fetchone()
    return row is not None and row[0] != ClassResult.Status.DRAFT


def rows_for(arm: ClassArm, d: date, *, term: Term) -> list[Row]:
    """The register as shown: each listed student with their status for ``d``.

    One query, plus one for the other arms' names when a moved student was recorded elsewhere.
    """
    students = _listed_with_records(arm, d, term)
    other_ids = {s.rec_arm_id for s in students if s.rec_id and s.rec_arm_id != arm.pk}
    others = ClassArm.objects.select_related("level").in_bulk(other_ids) if other_ids else {}
    return [
        Row(
            s.student,
            s.rec_status,
            others.get(s.rec_arm_id) if s.rec_id and s.rec_arm_id != arm.pk else None,
        )
        for s in students
    ]


def _validate(given: dict[int, str], required: list[int]) -> None:
    errors = []
    if set(required) - set(given):
        errors.append("Give a status for every student on the register.")
    if set(given) - set(required):
        errors.append("Some students are not on this register.")
    if any(v not in STATUSES for v in given.values()):
        errors.append("Use present, absent, late or excused.")
    if errors:
        raise ValidationError({"records": errors})


def save_register(
    arm: ClassArm,
    d: date,
    statuses: dict[int, str],
    *,
    by: Any,
    request: Any = None,
    term: Term | None = None,
) -> SaveResult:
    """Save ``arm``'s register for ``d`` from ``statuses`` (``{student_id: status}``).

    The keys must be exactly the register's students, leaving out any student whose record for
    ``d`` was taken in another arm: such entries are ignored and that record is never
    overwritten. Raises ``RegisterDateError``, ``RegisterLocked`` or ``ValidationError``.
    Permission (``can_take``/``can_edit``) is the caller's job. ``term`` is the current term
    when the caller already has it (the date is still re-checked under the locks).
    """
    with transaction.atomic():
        # The holiday save takes the holidays lock too, so holidays first, then this register.
        school_lock("attendance:holidays")
        school_lock(f"attendance:{arm.pk}:{d.isoformat()}")
        # Re-checked under the locks: a holiday may have been added since the caller looked.
        term = check_register_date(d, term=term)
        if _locked_for_save(arm, term):
            raise RegisterLocked()

        listed = _listed_with_records(arm, d, term)
        mine = [s for s in listed if s.rec_id is None or s.rec_arm_id == arm.pk]
        mine_ids = {s.pk for s in mine}
        elsewhere = {s.pk for s in listed} - mine_ids
        given = {k: v for k, v in statuses.items() if k not in elsewhere}
        _validate(given, [s.pk for s in mine])
        first_save = all(s.rec_id is None for s in mine)

        result = SaveResult()
        now = timezone.now()
        to_create: list[AttendanceRecord] = []
        to_update: list[AttendanceRecord] = []
        for s in mine:
            new = given[s.pk]
            if s.rec_id is None:
                rec = AttendanceRecord(student_id=s.pk, date=d, arm=arm, status=new, recorded_by=by)
                to_create.append(rec)
                result.changed[s.pk] = (None, new)
            elif s.rec_status != new:
                rec = AttendanceRecord(pk=s.rec_id, status=new, recorded_by=by, updated_at=now)
                to_update.append(rec)
                result.changed[s.pk] = (s.rec_status, new)
        if to_create:
            AttendanceRecord.objects.bulk_create(to_create)
        if to_update:
            AttendanceRecord.objects.bulk_update(to_update, ["status", "recorded_by", "updated_at"])
        result.created = len(to_create)
        result.newly_absent = [r.pk for r in to_create + to_update if r.status == ABSENT]

        if first_save and mine:
            counts = dict.fromkeys(STATUSES, 0)
            for s in mine:
                counts[given[s.pk]] += 1
            record(
                "attendance.register_saved",
                request=request,
                actor=by,
                obj=arm,
                after={"arm_id": arm.pk, "date": d.isoformat(), "counts": counts},
            )
        elif result.changed:
            record(
                "attendance.register_changed",
                request=request,
                actor=by,
                obj=arm,
                before={str(sid): old for sid, (old, _) in result.changed.items()},
                # "date" sits beside the {student_id: status} map; ids never collide with it.
                after={
                    "date": d.isoformat(),
                    **{str(sid): new for sid, (_, new) in result.changed.items()},
                },
            )

        if result.newly_absent:
            record_ids = list(result.newly_absent)
            transaction.on_commit(lambda: hooks.on_register_saved(record_ids))
    return result
