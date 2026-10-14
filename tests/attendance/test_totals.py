"""Term totals (P5 Task 4): one aggregate per batch, and what the report card shows."""

from datetime import date, timedelta

import pytest
from django_tenants.utils import schema_context

from apps.attendance.models import AttendanceRecord, AttendanceSettings
from apps.attendance.totals import CardAttendance, Totals, attendance_for_card, term_totals
from apps.results.models import ReportComment
from tests.attendance.factories import TEN_DAYS, add_records
from tests.fees.factories import make_staff, make_terms
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = make_terms()
        world.term = world.term1  # 1 Sep to 15 Dec 2026
        world.arm = world.level.arms.get(suffix="A")
        world.by = make_staff("teacher")
        world.students = [StudentProfileFactory(enrolment__arm=world.arm) for _ in range(3)]
        yield world


def _threshold(n: int) -> None:
    s = AttendanceSettings.load()
    s.late_threshold = n
    s.save()


def test_totals_over_ten_school_days(w):
    s = w.students[0]
    add_records(s, w.arm, w.term.start_date, TEN_DAYS, by=w.by)
    assert term_totals([s.pk], w.term) == {
        s.pk: Totals(days_opened=10, days_present=9, times_late=2, times_absent=1, times_excused=0)
    }


def test_excused_is_opened_but_not_present(w):
    s = w.students[0]
    add_records(s, w.arm, w.term.start_date, ["present", "excused", "absent"], by=w.by)
    assert term_totals([s.pk], w.term)[s.pk] == Totals(3, 1, 0, 1, 1)


def test_students_without_records_are_absent_from_the_dict(w):
    a, b, c = w.students
    add_records(a, w.arm, w.term.start_date, ["present"], by=w.by)
    assert set(term_totals([a.pk, b.pk, c.pk], w.term)) == {a.pk}
    assert term_totals([], w.term) == {}


def test_only_dates_inside_the_term_count_whatever_the_arm(w):
    """Records outside the term are ignored; records taken in another arm (a move) count."""
    from apps.academics.models import ClassArm

    s = w.students[0]
    other_arm, _ = ClassArm.objects.get_or_create(level=w.level, suffix="B")
    add_records(s, w.arm, w.term.start_date, ["present", "late"], by=w.by)
    add_records(s, other_arm, date(2026, 9, 7), ["absent"], by=w.by)  # after the move
    add_records(s, w.arm, w.term.start_date - timedelta(days=7), ["absent"], by=w.by)  # before
    add_records(s, w.arm, w.term2.start_date, ["absent"], by=w.by)  # next term
    assert term_totals([s.pk], w.term)[s.pk] == Totals(3, 2, 1, 1, 0)
    assert term_totals([s.pk], w.term2)[s.pk] == Totals(1, 0, 0, 1, 0)


def test_term_totals_for_forty_students_is_one_query(w):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    students = w.students + [StudentProfileFactory(enrolment__arm=w.arm) for _ in range(37)]
    AttendanceRecord.objects.bulk_create(
        AttendanceRecord(
            student=s,
            date=w.term.start_date + timedelta(days=d),
            arm=w.arm,
            status="late" if (i + d) % 4 == 0 else "present",
            recorded_by=w.by,
        )
        for i, s in enumerate(students)
        for d in (0, 1, 2)  # Tue, Wed, Thu
    )
    ids = [s.pk for s in students]
    with CaptureQueriesContext(connection) as ctx:
        totals = term_totals(ids, w.term)
    # django-tenants may add a SET search_path; that is not a query of ours.
    real = [q for q in ctx.captured_queries if not q["sql"].startswith("SET search_path")]
    assert len(real) == 1
    assert len(totals) == 40
    assert all(t.days_opened == 3 for t in totals.values())


@pytest.mark.parametrize("threshold,shown", [(3, None), (2, 2), (1, 2), (0, None)])
def test_late_line_follows_the_threshold(w, threshold, shown):
    s = w.students[0]
    add_records(s, w.arm, w.term.start_date, TEN_DAYS, by=w.by)
    _threshold(threshold)
    assert attendance_for_card(s, w.term) == CardAttendance(
        days_present=9, days_opened=10, times_late_shown=shown, source="register"
    )


def test_default_threshold_is_three(w):
    s = w.students[0]
    add_records(s, w.arm, w.term.start_date, ["late"] * 3, by=w.by)
    assert attendance_for_card(s, w.term).times_late_shown == 3


def test_register_beats_typed_numbers(w):
    s = w.students[0]
    ReportComment.objects.create(student=s, term=w.term, days_present=58, days_open=60)
    add_records(s, w.arm, w.term.start_date, TEN_DAYS, by=w.by)
    att = attendance_for_card(s, w.term)
    assert (att.source, att.days_present, att.days_opened) == ("register", 9, 10)


def test_manual_fallback_and_neither(w):
    a, b, c = w.students
    ReportComment.objects.create(student=a, term=w.term, days_present=58, days_open=60)
    ReportComment.objects.create(student=b, term=w.term, days_present=None, days_open=60)
    assert attendance_for_card(a, w.term) == CardAttendance(58, 60, None, "manual")
    assert attendance_for_card(b, w.term) == CardAttendance(None, None, None, None)
    assert attendance_for_card(c, w.term) == CardAttendance(None, None, None, None)
