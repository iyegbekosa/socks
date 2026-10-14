"""Register fixtures. Call inside ``schema_context(...)``."""

from datetime import date
from types import SimpleNamespace
from unittest import mock

from apps.academics.models import ClassArm, FormTeacher, Term
from tests.fees.factories import make_staff, make_terms
from tests.students.factories import StudentProfileFactory

TODAY = date(2026, 1, 12)  # a Monday inside the term


def today(d: date = TODAY):
    """Fix ``timezone.localdate()`` (the Africa/Lagos school date)."""
    return mock.patch("django.utils.timezone.localdate", return_value=d)


def make_register_world(n: int = 3) -> SimpleNamespace:
    """Current term Mon 5 Jan to Fri 16 Jan 2026; arm A with ``n`` students and a form
    teacher; an empty arm B with its own form teacher."""
    w = make_terms()
    Term.objects.filter(is_current=True).update(is_current=False)
    Term.objects.filter(pk=w.term2.pk).update(
        start_date=date(2025, 9, 1), end_date=date(2025, 12, 15)
    )
    Term.objects.filter(pk=w.term1.pk).update(
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 16), is_current=True
    )
    w.term1.refresh_from_db()
    arm = w.level.arms.get(suffix="A")
    arm_b, _ = ClassArm.objects.get_or_create(level=w.level, suffix="B")
    teacher = make_staff("teacher")
    teacher_b = make_staff("teacher")
    FormTeacher.objects.create(arm=arm, session=w.session, teacher=teacher)
    FormTeacher.objects.create(arm=arm_b, session=w.session, teacher=teacher_b)
    students = [
        StudentProfileFactory(last_name=f"Student{i:03d}", enrolment__arm=arm) for i in range(n)
    ]
    return SimpleNamespace(
        session=w.session,
        term=w.term1,
        arm=arm,
        arm_b=arm_b,
        teacher=teacher,
        teacher_b=teacher_b,
        students=students,
    )


TEN_DAYS = ["present"] * 7 + ["late"] * 2 + ["absent"]  # 9 present of 10, late twice


def add_records(student, arm, start: date, statuses, *, by=None) -> list:
    """One record per status on consecutive weekdays from ``start`` (no school-day checks)."""
    from datetime import timedelta

    from apps.attendance.models import AttendanceRecord

    if by is None:
        by = make_staff("teacher")
    d, out = start, []
    for status in statuses:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            AttendanceRecord.objects.create(
                student=student, date=d, arm=arm, status=status, recorded_by=by
            )
        )
        d += timedelta(days=1)
    return out
