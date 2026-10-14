import csv
import io
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, ClassArm, Term
from apps.attendance.models import AttendanceRecord, Holiday
from apps.students.models import Enrolment
from tests.accounts.helpers import login_verified
from tests.attendance.factories import (
    TODAY,
    add_records,
    make_register_world,
    today,
)
from tests.attendance.test_register import NOISE
from tests.fees.factories import make_staff
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

START = date(2026, 1, 5)


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield make_register_world()


def _class_url(arm, query=""):
    return reverse("attendance:class", args=[arm.pk]) + query


def _staff_login(client, role):
    with schema_context("school_a"):
        user = make_staff(role)
    login_verified(client, user)
    return user


def test_urls():
    assert reverse("attendance:class", args=[3]) == "/attendance/class/3/"
    assert reverse("attendance:school") == "/attendance/school/"
    assert reverse("attendance:me") == "/attendance/me/"


def test_class_grid_letters_and_totals(client_a, w):
    s0, s1, _s2 = w.students
    with schema_context("school_a"):
        add_records(
            s0, w.arm, START, ["present", "absent", "late", "excused", "present", "present"]
        )
        add_records(s1, w.arm, START, ["present", "present"])
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm))
    assert r.status_code == 200
    html = r.content.decode()
    assert 'data-cell="P"' in html and 'data-cell="A"' in html
    assert 'data-cell="L"' in html and 'data-cell="E"' in html
    rows = r.context["rows"]
    by_pk = {row["student"].pk: row for row in rows}
    assert [c["letter"] for c in by_pk[s0.pk]["cells"]] == ["P", "A", "L", "E", "P", "P"]
    assert [c["letter"] for c in by_pk[s1.pk]["cells"]] == ["P", "P", "·", "·", "·", "·"]
    t = by_pk[s0.pk]["totals"]
    assert (t.days_opened, t.days_present, t.times_late, t.times_absent, t.times_excused) == (
        6,
        4,
        1,
        1,
        1,
    )
    # Days 3-6 have records for s0, so only none are missing.
    assert r.context["missing"] == []


def test_class_missing_registers_highlighted(client_a, w):
    with schema_context("school_a"):
        add_records(w.students[0], w.arm, START, ["present", "present"])
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm))
    assert r.context["missing"] == [date(2026, 1, d) for d in (7, 8, 9, 12)]
    assert r.content.decode().count('class="missing"') >= 4


def test_class_holiday_is_not_a_column(client_a, w):
    with schema_context("school_a"):
        Holiday.objects.create(date=date(2026, 1, 6), name="Break")
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm))
    assert date(2026, 1, 6) not in r.context["days"]
    assert len(r.context["days"]) == 5


def test_class_access(client_a, w):
    url = _class_url(w.arm)
    with schema_context("school_a"):
        client_a.force_login(w.teacher_b)
    with today():
        assert client_a.get(url).status_code == 403
    with schema_context("school_a"):
        client_a.force_login(w.students[0].user)
    with today():
        assert client_a.get(url).status_code == 403
    _staff_login(client_a, "bursar")
    with today():
        assert client_a.get(url).status_code == 403
    for role in ("principal", "school_admin", "proprietor"):
        _staff_login(client_a, role)
        with today():
            assert client_a.get(url).status_code == 200, role
    with today():
        assert client_a.get("/attendance/class/abc/").status_code == 404
        assert client_a.get("/attendance/class/999999/").status_code == 404


def test_class_other_school_arm_404(client_a, school_b, w):
    with schema_context("school_a"):
        top = ClassArm.objects.order_by("-pk").first().pk
    with schema_context("school_b"):
        arm_b = make_register_world().arm
        level = arm_b.level
        i = 0
        while arm_b.pk <= top:  # pad so the id cannot exist in school A
            i += 1
            arm_b = ClassArm.objects.create(level=level, suffix=f"Pad{i}")
    _staff_login(client_a, "principal")
    with today():
        assert client_a.get(_class_url(arm_b)).status_code == 404


def test_class_term_other_session_falls_back(client_a, w):
    with schema_context("school_a"):
        other = AcademicSession.objects.create(
            name="2030/2031", start_date=date(2030, 9, 1), end_date=date(2031, 7, 1)
        )
        foreign = Term.objects.create(
            session=other, number=1, start_date=date(2030, 9, 1), end_date=date(2030, 12, 15)
        )
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm, f"?term={foreign.pk}"))
    assert r.status_code == 200
    assert r.context["term"].pk == w.term.pk


def test_school_page_omits_arm_with_only_withdrawn_students(client_a, w):
    with schema_context("school_a"):
        from apps.students.models import Enrolment

        StudentProfileFactory(enrolment__arm=w.arm_b)
        Enrolment.objects.filter(arm=w.arm_b).update(status=Enrolment.Status.WITHDRAWN)
    _staff_login(client_a, "principal")
    with today():
        r = client_a.get(reverse("attendance:school"))
    assert [x["arm"].pk for x in r.context["rows"]] == [w.arm.pk]


def test_class_term_param_falls_back(client_a, w):
    with schema_context("school_a"):
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm, "?term=zzz"))
        assert r.context["term"].pk == w.term.pk
        r = client_a.get(_class_url(w.arm, "?term=999999"))
        assert r.context["term"].pk == w.term.pk


def test_class_moved_student_listed_with_arrow(client_a, w):
    s0 = w.students[0]
    with schema_context("school_a"):
        add_records(s0, w.arm, START, ["present", "present"])
        # s0 moves to arm B: later records are taken there.
        add_records(s0, w.arm_b, date(2026, 1, 7), ["absent"])
        from apps.students.models import Enrolment

        Enrolment.objects.filter(student=s0).update(arm=w.arm_b)
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm))
    rows = {row["student"].pk: row for row in r.context["rows"]}
    assert s0.pk in rows  # still listed: has records in this arm
    cells = rows[s0.pk]["cells"]
    assert [c["letter"] for c in cells[:3]] == ["P", "P", "↗"]
    assert w.arm_b.display_name in cells[2]["title"]
    assert "↗" in r.content.decode()


def test_class_csv_guards_formulas(client_a, w):
    with schema_context("school_a"):
        evil = StudentProfileFactory(first_name="=cmd", last_name="Evil", enrolment__arm=w.arm)
        add_records(evil, w.arm, START, ["present", "absent", "late"])
        client_a.force_login(w.teacher)
    with today():
        r = client_a.get(_class_url(w.arm, "?format=csv"))
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/csv")
    assert r["Content-Disposition"].startswith("attachment")
    assert r["Cache-Control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0][:2] == ["Admission no.", "Name"]
    assert rows[0][2:8] == [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12)]
    assert rows[0][8:] == ["Present", "Opened", "Late", "Absent", "Excused"]
    (row,) = [x for x in rows[1:] if "Evil" in x[1]]
    assert row[1].startswith("'=")
    assert row[2:5] == ["P", "A", "L"]
    assert row[8:] == ["2", "3", "1", "1", "0"]


def test_class_query_count_bounded(client_a, school_a):
    with schema_context("school_a"):
        w = make_register_world(40)
        by = make_staff("teacher")
        from apps.attendance.days import school_days

        # 60 days: stretch the term so 60 weekdays have passed.
        w.term.__class__.objects.filter(pk=w.term.pk).update(
            start_date=date(2025, 10, 6), end_date=date(2026, 3, 31)
        )
        w.term.refresh_from_db()
        days = [d for d in school_days(w.term, until=TODAY)][:60]
        assert len(days) == 60
        AttendanceRecord.objects.bulk_create(
            AttendanceRecord(student=s, date=d, arm=w.arm, status="present", recorded_by=by)
            for s in w.students
            for d in days
        )
        # A student who moved out but has records here: the extra-student path.
        mover = w.students[0]
        Enrolment.objects.filter(student=mover).update(arm=w.arm_b)
        AttendanceRecord.objects.filter(student=mover, date=days[-1]).update(arm=w.arm_b)
        client_a.force_login(w.teacher)
    url = _class_url(w.arm, f"?term={w.term.pk}")
    with today():
        client_a.get(reverse("home"))
        with CaptureQueriesContext(connection) as base:
            client_a.get("/attendance/class/x/")
        with CaptureQueriesContext(connection) as ctx:
            r = client_a.get(url)
    assert r.status_code == 200
    real = lambda c: [q for q in c.captured_queries if not q["sql"].startswith(NOISE)]  # noqa: E731
    n = len(real(ctx)) - len(real(base))
    assert n <= 10, n


def test_school_page_unregistered_first_and_counts(client_a, w):
    s0, s1, s2 = w.students
    with schema_context("school_a"):
        # Arm A registered today: 1 present, 1 absent, 1 late. Arm B has no register.
        add_records(s0, w.arm, TODAY, ["present"])
        add_records(s1, w.arm, TODAY, ["absent"])
        add_records(s2, w.arm, TODAY, ["late"])
        add_records(s0, w.arm, START, ["present", "absent"])  # earlier days
        StudentProfileFactory(enrolment__arm=w.arm_b)
    _staff_login(client_a, "principal")
    with today():
        r = client_a.get(reverse("attendance:school"))
    assert r.status_code == 200
    rows = r.context["rows"]
    assert [x["arm"].pk for x in rows] == [w.arm_b.pk, w.arm.pk]
    a = rows[1]
    assert (a["registered"], a["present"], a["absent"], a["late"]) == (3, 2, 1, 1)
    # Term to date: records 3 today + 2 earlier = 5 opened; present: P, L, P(0105) = 3
    assert a["percent"] == "60.0"
    assert rows[0]["registered"] == 0 and rows[0]["percent"] == "—"


def test_school_page_roles(client_a, w):
    url = reverse("attendance:school")
    for role, code in (("principal", 200), ("school_admin", 200), ("proprietor", 200)):
        _staff_login(client_a, role)
        with today():
            assert client_a.get(url).status_code == code
    _staff_login(client_a, "bursar")
    with today():
        assert client_a.get(url).status_code == 403
    with schema_context("school_a"):
        client_a.force_login(w.teacher)
    with today():
        assert client_a.get(url).status_code == 403


def test_school_page_dates(client_a, w):
    _staff_login(client_a, "principal")
    url = reverse("attendance:school")
    with today():
        r = client_a.get(url + "?date=2026-01-10")  # Saturday
        html = r.content.decode()
        assert "not a school day" in html
        assert r.context["rows"] == []
        r = client_a.get(url + "?date=garbage")
        assert r.status_code == 200
        assert r.context["day"] == TODAY
        assert "valid date" in r.content.decode()


def test_my_attendance(client_a, w):
    s0, s1, _ = w.students
    with schema_context("school_a"):
        add_records(s0, w.arm, START, ["present", "absent", "late", "excused"])
        add_records(s1, w.arm, START, ["absent", "absent"])
        client_a.force_login(s0.user)
    r = client_a.get(reverse("attendance:me"))
    assert r.status_code == 200
    html = r.content.decode()
    assert "6 January 2026" in html  # own absent date
    assert "7 January 2026" in html  # own late date
    assert "5 January 2026" not in html  # s1's absence on the 5th, and mine was present
    assert "8 January 2026" not in html
    t = r.context["totals"]
    assert (t.days_opened, t.days_present, t.times_excused) == (4, 2, 1)


def test_my_attendance_students_only(client_a, w):
    with schema_context("school_a"):
        client_a.force_login(w.teacher)
    assert client_a.get(reverse("attendance:me")).status_code == 403


def test_my_attendance_no_current_term(client_a, w):
    with schema_context("school_a"):
        from apps.academics.models import Term

        Term.objects.update(is_current=False)
        client_a.force_login(w.students[0].user)
    assert "No current term." in client_a.get(reverse("attendance:me")).content.decode()


def test_home_links(client_a, w):
    me = reverse("attendance:me")
    school = reverse("attendance:school")
    with schema_context("school_a"):
        s0 = w.students[0]
        client_a.force_login(s0.user)
    assert me not in client_a.get(reverse("home")).content.decode()
    with schema_context("school_a"):
        add_records(s0, w.arm, START, ["present", "absent"])
    html = client_a.get(reverse("home")).content.decode()
    assert me in html and "Attendance: 1 / 2 days this term" in html
    _staff_login(client_a, "principal")
    assert school in client_a.get(reverse("home")).content.decode()
    with schema_context("school_a"):
        client_a.force_login(w.teacher)
    home = client_a.get(reverse("home")).content.decode()
    assert school not in home
    assert reverse("attendance:class", args=[w.arm.pk]) in home
    with today():
        reg = client_a.get(reverse("attendance:register", args=[w.arm.pk, "2026-01-12"]))
    assert reverse("attendance:class", args=[w.arm.pk]) in reg.content.decode()
