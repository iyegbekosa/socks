"""A student who moves arm mid-term (final review I1).

The summary's arm is the single source of truth for a (student, term): the card, My results
and the broadsheet show only that arm's subjects, and computing an arm removes its students'
computed rows from any other arm for the term.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel
from apps.results.cards import card_context
from apps.results.compute import compute_class, prior_term_totals, stats
from apps.results.models import (
    ClassResult,
    ResultSummary,
    SubjectResult,
    subject_results_for,
)
from apps.results.views.broadsheet import broadsheet_data
from apps.students.models import Enrolment
from tests.results.factories import accuracy_world, approve_and_compute
from tests.results.test_accuracy import _enter, _offerings, _submit, _term
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

B_SUBJECTS = ["Mathematics", "Civic Education"]


@pytest.fixture
def moved(school_a):
    """Arm A (the accuracy class) computed and published with Pupil A; then Pupil A moves to
    arm B, which has its own Mathematics and Civic Education and one other student."""
    with schema_context("school_a"):
        w = accuracy_world()
        approve_and_compute(w)
        ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="published")
        level = ClassLevel.objects.get(pk=w.arm.level_id)
        w.arm_b = ClassArm.objects.create(level=level, suffix="B")
        w.off_b = _offerings(w.arm_b, w.session, w.teacher, B_SUBJECTS)
        w.mover = w.students["A"]
        Enrolment.objects.filter(student=w.mover, session=w.session).update(arm=w.arm_b)
        w.other = StudentProfileFactory(first_name="Pupil", last_name="Z", enrolment__arm=w.arm_b)
        for st, rows in ((w.mover, [(30, 40), (20, 50)]), (w.other, [(10, 30), (25, 45)])):
            for name, values in zip(B_SUBJECTS, rows, strict=True):
                _enter(st, w.off_b[name], w.term, w.comps, values, w.teacher)
        _submit(w.off_b.values(), w.term)
        yield w


def _b_offering_ids(w):
    return {o.pk for o in w.off_b.values()}


def test_computing_new_arm_removes_old_arm_rows_for_moved_student(moved):
    w = moved
    assert SubjectResult.objects.filter(student=w.mover, term=w.term).count() == 3
    compute_class(w.arm_b, w.term)
    mine = SubjectResult.objects.filter(student=w.mover, term=w.term)
    assert set(mine.values_list("offering_id", flat=True)) == _b_offering_ids(w)
    summary = ResultSummary.objects.get(student=w.mover, term=w.term)
    assert summary.arm_id == w.arm_b.pk
    # Other students of the old arm keep their rows.
    assert SubjectResult.objects.filter(student=w.students["B"], term=w.term).count() == 3


def test_card_my_results_and_broadsheet_show_only_the_new_arm(client_a, moved):
    w = moved
    compute_class(w.arm_b, w.term)
    ClassResult.objects.update_or_create(arm=w.arm_b, term=w.term, defaults={"status": "published"})
    # Even a stray old-arm row (e.g. left before this fix) must not reach any reader.
    stray = SubjectResult.objects.filter(student=w.students["B"], term=w.term).first()
    SubjectResult.objects.create(
        student=w.mover,
        offering=stray.offering,
        term=w.term,
        subject_name="Basic Science",
        components=[],
        total=Decimal("99"),
        grade="A",
        position=1,
        highest=Decimal("99"),
        lowest=Decimal("1"),
        class_average=Decimal("50"),
    )
    summary = ResultSummary.objects.get(student=w.mover, term=w.term)
    assert set(subject_results_for(summary).values_list("offering_id", flat=True)) == (
        _b_offering_ids(w)
    )

    ctx = card_context(w.mover, w.term, logo=None)
    assert sorted(r["subject"] for r in ctx["rows"]) == sorted(B_SUBJECTS)
    assert sum(r["total"] for r in ctx["rows"]) == ctx["total"] == summary.total
    assert ctx["class_name"] == w.arm_b.display_name

    client_a.force_login(w.mover.user)
    r = client_a.get(reverse("results:my_results"))
    [entry] = r.context["terms"]
    assert sorted(s.subject_name for s in entry["subjects"]) == sorted(B_SUBJECTS)
    assert entry["summary"].total == summary.total

    sheet = broadsheet_data(w.arm_b, w.term)
    [row] = [row for row in sheet["rows"] if row["student_id"] == w.mover.pk]
    assert sorted(s["name"] for s in sheet["subjects"]) == sorted(B_SUBJECTS)
    assert sum(t for t in row["totals"] if t is not None) == summary.total
    old = broadsheet_data(w.arm, w.term)
    assert w.mover.pk not in [row["student_id"] for row in old["rows"]]


def test_recomputing_old_arm_drops_moved_student_from_its_stats(moved):
    w = moved
    compute_class(w.arm_b, w.term)
    compute_class(w.arm, w.term)
    remaining = [st for letter, st in w.students.items() if letter != "A"]
    summaries = ResultSummary.objects.filter(arm=w.arm, term=w.term)
    assert {s.student_id for s in summaries} == {st.pk for st in remaining}
    assert {s.out_of for s in summaries} == {5}
    maths = w.offerings["Mathematics"]
    rows = list(SubjectResult.objects.filter(offering=maths, term=w.term))
    assert w.mover.pk not in {r.student_id for r in rows}
    highest, lowest, average = stats([r.total for r in rows])
    assert {(r.highest, r.lowest, r.class_average) for r in rows} == {(highest, lowest, average)}
    sheet = broadsheet_data(w.arm, w.term)
    assert w.mover.pk not in [row["student_id"] for row in sheet["rows"]]
    col = [s["name"] for s in sheet["subjects"]].index("Mathematics")
    footer = {f["label"]: f["values"] for f in sheet["footer"]}
    assert (footer["Highest"][col], footer["Lowest"][col], footer["Average"][col]) == (
        highest,
        lowest,
        average,
    )
    # The moved student's own rows stay with arm B.
    assert set(
        SubjectResult.objects.filter(student=w.mover, term=w.term).values_list(
            "offering_id", flat=True
        )
    ) == _b_offering_ids(w)


def test_prior_term_totals_use_the_summary_arm_only(moved):
    """A 3rd-term cumulative reads each earlier term's totals from that term's summary arm,
    so a stray row from another arm cannot make it depend on row order."""
    w = moved
    compute_class(w.arm_b, w.term)
    term3 = _term(w.session, 3)
    maths_a = w.offerings["Mathematics"]
    SubjectResult.objects.create(
        student=w.mover,
        offering=maths_a,
        term=w.term,
        subject_name="Mathematics",
        components=[],
        total=Decimal("12"),
        grade="F",
        position=6,
        highest=Decimal("12"),
        lowest=Decimal("12"),
        class_average=Decimal("12"),
    )
    found = prior_term_totals([w.mover.pk], term3)
    subject_id = maths_a.subject_id
    assert found[(w.mover.pk, w.term.pk, subject_id)] == Decimal("70")  # arm B: 30 + 40
    ResultSummary.objects.filter(student=w.mover, term=w.term).update(
        arm=w.arm, computed_at=timezone.now()
    )
    found = prior_term_totals([w.mover.pk], term3)
    assert found[(w.mover.pk, w.term.pk, subject_id)] == Decimal("12")


def test_staff_do_not_get_the_old_arms_card(moved):
    from apps.results.cards import downloadable_card
    from apps.results.models import ReportCard

    w = moved
    card = ReportCard.objects.create(
        student=w.mover, term=w.term, arm=w.arm, file_name="x.pdf", generated_at=timezone.now()
    )
    assert downloadable_card(w.principal, w.mover.pk, w.term.pk) == card
    compute_class(w.arm_b, w.term)
    assert downloadable_card(w.principal, w.mover.pk, w.term.pk) is None
    ReportCard.objects.filter(pk=card.pk).update(arm=w.arm_b)
    assert downloadable_card(w.principal, w.mover.pk, w.term.pk) == card
