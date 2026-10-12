"""The hand-calculated broadsheet (FR-07): every value must match exactly."""

import logging
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, Subject, SubjectOffering, Term
from apps.results.compute import (
    ComponentSpec,
    OfferingSpec,
    compute_class,
    compute_class_data,
    grade_lookup,
)
from apps.results.models import (
    AssessmentComponent,
    ClassResult,
    GradeBand,
    ResultSummary,
    Score,
    ScoreSheet,
    SubjectResult,
)
from apps.results.seeding import BASIC_SCALE
from apps.students.models import Enrolment, Student
from tests.accounts.factories import StaffFactory
from tests.students.factories import StudentProfileFactory, default_arm, default_session

pytestmark = pytest.mark.django_db

D = Decimal
SUBJECTS = ["Mathematics", "English Language", "Basic Science"]
# (CA, Exam) per subject, in SUBJECTS order.
SCORES = {
    "A": [(30, 50), (28, 42), (32, 48)],
    "B": [(35, 45), (30, 40), (30, 40)],
    "C": [(20, 40), (35, 50), (30, 45)],
    "D": [(25, 30), (20, 25), (30, 30)],
    "E": [(10, 20), (15, 30), (20, 24)],
    "F": [(40, 58), (25, 40), (38, 55)],
}
# (total, grade, position) per subject.
EXPECTED_SUBJECTS = {
    "A": [("80", "A", 2), ("70", "A", 2), ("80", "A", 2)],
    "B": [("80", "A", 2), ("70", "A", 2), ("70", "A", 4)],
    "C": [("60", "B", 4), ("85", "A", 1), ("75", "A", 3)],
    "D": [("55", "C", 5), ("45", "D", 5), ("60", "B", 5)],
    "E": [("30", "F", 6), ("45", "D", 5), ("44", "E", 6)],
    "F": [("98", "A", 1), ("65", "B", 4), ("93", "A", 1)],
}
EXPECTED_REMARKS = {"A": "Excellent", "B": "Very good", "C": "Good", "D": "Fair", "E": "Poor"}
EXPECTED_REMARKS["F"] = "Fail"
# (highest, lowest, average)
EXPECTED_STATS = {
    "Mathematics": ("98", "30", "67.17"),
    "English Language": ("85", "45", "63.33"),
    "Basic Science": ("93", "44", "70.33"),
}
# (total, average, position)
EXPECTED_SUMMARY = {
    "F": ("256", "85.33", 1),
    "A": ("230", "76.67", 2),
    "B": ("220", "73.33", 3),
    "C": ("220", "73.33", 3),
    "D": ("160", "53.33", 5),
    "E": ("119", "39.67", 6),
}


def _term(session, number, current=False):
    starts = {1: date(2026, 9, 1), 2: date(2027, 1, 5), 3: date(2027, 4, 20)}
    ends = {1: date(2026, 12, 15), 2: date(2027, 4, 1), 3: date(2027, 7, 20)}
    term, _ = Term.objects.update_or_create(
        session=session,
        number=number,
        defaults={"start_date": starts[number], "end_date": ends[number], "is_current": current},
    )
    return term


def _jss_structure():
    """CA 40 + Exam 60 (a valid override of the default) and the default JSS scale."""
    Score.objects.all().delete()
    AssessmentComponent.objects.filter(section="jss").delete()
    ca = AssessmentComponent.objects.create(section="jss", name="CA", max_score=40, order=1)
    exam = AssessmentComponent.objects.create(section="jss", name="Exam", max_score=60, order=2)
    GradeBand.objects.filter(section="jss").delete()
    for lower, upper, grade, remark in BASIC_SCALE:
        GradeBand.objects.create(
            section="jss", lower=lower, upper=upper, grade=grade, remark=remark
        )
    return ca, exam


def _offerings(arm, session, teacher, names):
    out = {}
    for name in names:
        subject, _ = Subject.objects.get_or_create(
            name=name, defaults={"code": name.lower().replace(" ", "-"), "sections": ["jss"]}
        )
        out[name] = SubjectOffering.objects.create(
            subject=subject, arm=arm, session=session, teacher=teacher
        )
    return out


def _enter(student, offering, term, comps, values, teacher):
    for comp, value in zip(comps, values, strict=True):
        Score.objects.create(
            student=student,
            offering=offering,
            term=term,
            component=comp,
            value=D(value),
            entered_by=teacher,
            version=1,
        )


def _submit(offerings, term):
    for off in offerings:
        ScoreSheet.objects.update_or_create(
            offering=off, term=term, defaults={"status": ScoreSheet.Status.SUBMITTED}
        )


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        Term.objects.filter(is_current=True).update(is_current=False)
        session = default_session()
        term = _term(session, 1, current=True)
        arm = default_arm()
        comps = list(_jss_structure())
        teacher = StaffFactory(first_name="Ada", last_name="Okafor")
        offerings = _offerings(arm, session, teacher, SUBJECTS)
        students = {}
        for letter, rows in SCORES.items():
            st = StudentProfileFactory(first_name="Pupil", last_name=letter)
            students[letter] = st
            for name, values in zip(SUBJECTS, rows, strict=True):
                _enter(st, offerings[name], term, comps, values, teacher)
        _submit(offerings.values(), term)
        yield SimpleNamespace(
            session=session,
            term=term,
            arm=arm,
            comps=comps,
            teacher=teacher,
            offerings=offerings,
            students=students,
        )


def _subject_row(w, letter, name):
    return SubjectResult.objects.get(
        student=w.students[letter], offering=w.offerings[name], term=w.term
    )


def test_accuracy_broadsheet(w):
    compute_class(w.arm, w.term)

    assert SubjectResult.objects.filter(term=w.term).count() == 18
    for letter, rows in EXPECTED_SUBJECTS.items():
        for name, (total, grade, position), (ca, exam) in zip(
            SUBJECTS, rows, SCORES[letter], strict=True
        ):
            r = _subject_row(w, letter, name)
            assert (r.total, r.grade, r.position) == (D(total), grade, position), (letter, name)
            assert r.remark == EXPECTED_REMARKS[grade]
            highest, lowest, average = EXPECTED_STATS[name]
            assert (r.highest, r.lowest, r.class_average) == (D(highest), D(lowest), D(average))
            assert str(r.class_average) == average
            assert r.cumulative is None
            assert r.subject_name == name
            assert r.teacher_name == "Ada Okafor"
            assert r.components == [
                {"name": "CA", "max": 40, "score": f"{ca}.0"},
                {"name": "Exam", "max": 60, "score": f"{exam}.0"},
            ]

    assert ResultSummary.objects.filter(term=w.term).count() == 6
    for letter, (total, average, position) in EXPECTED_SUMMARY.items():
        s = ResultSummary.objects.get(student=w.students[letter], term=w.term)
        assert (s.total, s.average, s.position, s.out_of) == (D(total), D(average), position, 6)
        assert str(s.average) == average
        assert s.subjects_count == 3
        assert s.arm_id == w.arm.pk
        assert s.cumulative_average is None
        assert s.computed_at is not None


def test_pure_core_matches_broadsheet():
    """The same dataset through compute_class_data, with no database."""
    comps = [ComponentSpec(id=1, name="CA", max_score=40), ComponentSpec(2, "Exam", 60)]
    offerings = [
        OfferingSpec(id=10 + i, subject_id=100 + i, subject_name=name, teacher_name="T")
        for i, name in enumerate(SUBJECTS)
    ]
    ids = {letter: n for n, letter in enumerate(SCORES, start=1)}
    scores = {
        ids[letter]: {
            off.id: {1: D(ca), 2: D(exam)} for off, (ca, exam) in zip(offerings, rows, strict=True)
        }
        for letter, rows in SCORES.items()
    }
    data = compute_class_data(
        student_ids=list(ids.values()),
        offerings=offerings,
        components=comps,
        scores=scores,
        grade=grade_lookup(BASIC_SCALE),
        term_number=1,
    )
    assert data.excluded == []
    by_key = {(r.student_id, r.offering_id): r for r in data.subjects}
    for letter, rows in EXPECTED_SUBJECTS.items():
        for off, (total, grade, position) in zip(offerings, rows, strict=True):
            r = by_key[(ids[letter], off.id)]
            assert (r.total, r.grade, r.position) == (D(total), grade, position)
            highest, lowest, average = EXPECTED_STATS[off.subject_name]
            assert (r.highest, r.lowest, r.class_average) == (D(highest), D(lowest), D(average))
    summaries = {s.student_id: s for s in data.summaries}
    for letter, (total, average, position) in EXPECTED_SUMMARY.items():
        s = summaries[ids[letter]]
        assert (s.total, s.average, s.position, s.out_of) == (D(total), D(average), position, 6)


def test_pure_core_cumulative():
    comps = [ComponentSpec(1, "CA", 40), ComponentSpec(2, "Exam", 60)]
    offerings = [OfferingSpec(10, 100, "Mathematics", ""), OfferingSpec(11, 101, "English", "")]
    data = compute_class_data(
        student_ids=[1],
        offerings=offerings,
        components=comps,
        scores={1: {10: {1: D(30), 2: D(50)}, 11: {1: D(25), 2: D(35)}}},
        grade=grade_lookup(BASIC_SCALE),
        term_number=3,
        prior_totals={1: {100: [D(60), D(70)], 101: [D(50)]}},
    )
    cum = {r.offering_id: r.cumulative for r in data.subjects}
    assert cum == {10: D("70.00"), 11: D("55.00")}
    assert data.summaries[0].cumulative_average == D("62.50")


def test_withdrawal_excludes_student(w):
    compute_class(w.arm, w.term)
    e = w.students["E"]
    Student.objects.filter(pk=e.pk).update(status=Student.Status.WITHDRAWN)
    Enrolment.objects.filter(student=e).update(status=Enrolment.Status.WITHDRAWN)

    compute_class(w.arm, w.term)

    assert not ResultSummary.objects.filter(student=e, term=w.term).exists()
    assert not SubjectResult.objects.filter(student=e, term=w.term).exists()
    summaries = ResultSummary.objects.filter(term=w.term)
    assert summaries.count() == 5
    assert {s.out_of for s in summaries} == {5}
    maths = SubjectResult.objects.filter(offering=w.offerings["Mathematics"], term=w.term)
    assert {(r.highest, r.lowest, r.class_average) for r in maths} == {
        (D("98"), D("55"), D("74.60"))
    }
    expected_positions = {"F": 1, "A": 2, "B": 3, "C": 3, "D": 5}
    for letter, position in expected_positions.items():
        s = summaries.get(student=w.students[letter])
        assert s.position == position


def test_withdrawn_status_only_also_excludes(w):
    e = w.students["E"]
    Student.objects.filter(pk=e.pk).update(status=Student.Status.WITHDRAWN)
    compute_class(w.arm, w.term)
    assert ResultSummary.objects.filter(term=w.term).count() == 5
    assert not ResultSummary.objects.filter(student=e).exists()


def test_atomic_write_keeps_previous_rows(w):
    compute_class(w.arm, w.term)
    before = {
        (s.student_id, s.total, s.average, s.position, s.out_of)
        for s in ResultSummary.objects.filter(term=w.term)
    }
    before_pks = set(SubjectResult.objects.values_list("pk", flat=True))
    assert len(before) == 6

    # Change a score so a recompute would write different numbers.
    Score.objects.filter(
        student=w.students["E"], offering=w.offerings["Mathematics"], component=w.comps[1]
    ).update(value=D("60"))
    real = ResultSummary.objects.bulk_create
    seen = {}

    def boom(*args, **kwargs):
        # SubjectResult rows were already rewritten in this transaction.
        seen["new"] = set(SubjectResult.objects.values_list("pk", flat=True)) - before_pks
        raise RuntimeError("disk full")

    with mock.patch.object(ResultSummary.objects, "bulk_create", side_effect=boom):
        with pytest.raises(RuntimeError):
            compute_class(w.arm, w.term)
    assert len(seen["new"]) == 18
    assert ResultSummary.objects.bulk_create == real

    after = {
        (s.student_id, s.total, s.average, s.position, s.out_of)
        for s in ResultSummary.objects.filter(term=w.term)
    }
    assert after == before
    assert set(SubjectResult.objects.values_list("pk", flat=True)) == before_pks


def test_requires_every_sheet_submitted(w):
    maths = w.offerings["Mathematics"]
    ScoreSheet.objects.filter(offering=maths, term=w.term).update(status="draft")
    with pytest.raises(ValidationError) as exc:
        compute_class(w.arm, w.term)
    assert str(maths.pk) in str(exc.value)
    assert "Pupil" not in str(exc.value)
    assert not ResultSummary.objects.exists()

    ScoreSheet.objects.filter(offering=maths, term=w.term).delete()
    with pytest.raises(ValidationError):
        compute_class(w.arm, w.term)


def test_requires_complete_components(w):
    AssessmentComponent.objects.create(section="jss", name="Project", max_score=10, order=3)
    with pytest.raises(ValidationError):
        compute_class(w.arm, w.term)
    assert not ResultSummary.objects.exists()


def test_blank_cell_for_a_taker_blocks(w):
    late = StudentProfileFactory(first_name="Pupil", last_name="G")
    with pytest.raises(ValidationError) as exc:
        compute_class(w.arm, w.term)
    message = str(exc.value)
    assert "Pupil" not in message and str(late.pk) not in message
    assert not ResultSummary.objects.exists()


def test_student_with_no_subjects_is_excluded_and_logged(w, caplog):
    maths = w.offerings["Mathematics"]
    SubjectOffering.objects.filter(pk__in=[o.pk for o in w.offerings.values()]).update(
        is_compulsory=False
    )
    # Every pupil but A chooses all three; A has no subjects at all.
    from apps.results.models import ElectiveChoice

    for letter, st in w.students.items():
        if letter != "A":
            for off in w.offerings.values():
                ElectiveChoice.objects.create(student=st, offering=off)
    with caplog.at_level(logging.WARNING, logger="apps.results.compute"):
        compute_class(w.arm, w.term)
    a = w.students["A"]
    assert not ResultSummary.objects.filter(student=a).exists()
    assert ResultSummary.objects.filter(term=w.term).count() == 5
    assert {s.out_of for s in ResultSummary.objects.all()} == {5}
    assert str(a.pk) in caplog.text
    assert "Pupil" not in caplog.text
    maths_rows = SubjectResult.objects.filter(offering=maths)
    # Without A: 98, 80, 60, 55, 30 -> average 64.60
    assert {r.class_average for r in maths_rows} == {D("64.60")}


def test_recompute_replaces_rows(w):
    compute_class(w.arm, w.term)
    compute_class(w.arm, w.term)
    assert SubjectResult.objects.filter(term=w.term).count() == 18
    assert ResultSummary.objects.filter(term=w.term).count() == 6


def test_reprs_show_pk_only(w):
    compute_class(w.arm, w.term)
    r = SubjectResult.objects.first()
    s = ResultSummary.objects.first()
    assert repr(r) == f"<SubjectResult {r.pk}>"
    assert repr(s) == f"<ResultSummary {s.pk}>"
    assert "Pupil" not in str(r) and "Pupil" not in str(s)


def test_cumulative_third_term(school_a):
    """Maths 60/70/80 -> 70.00; English 2nd/3rd only, 50/60 -> 55.00; average 62.50.

    The 1st term has no computed results (scores are read directly); the 2nd term is computed.
    """
    with schema_context("school_a"):
        Term.objects.filter(is_current=True).update(is_current=False)
        session = default_session()
        t1, t2, t3 = _term(session, 1), _term(session, 2), _term(session, 3, current=True)
        level, _ = ClassLevel.objects.get_or_create(
            name="JSS 3", defaults={"section": "jss", "order": 11}
        )
        arm = ClassArm.objects.create(level=level, suffix="C")
        comps = list(_jss_structure())
        teacher = StaffFactory()
        offs = _offerings(arm, session, teacher, ["Mathematics", "English Language"])
        maths, english = offs["Mathematics"], offs["English Language"]
        st = StudentProfileFactory(enrolment__arm=arm, enrolment__session=session)

        _enter(st, maths, t1, comps, (20, 40), teacher)  # 60, no English in the 1st term
        _enter(st, maths, t2, comps, (30, 40), teacher)  # 70
        _enter(st, english, t2, comps, (20, 30), teacher)  # 50
        _submit([maths, english], t2)
        compute_class(arm, t2)
        assert SubjectResult.objects.filter(term=t2).count() == 2
        assert SubjectResult.objects.get(term=t2, offering=maths).cumulative is None

        _enter(st, maths, t3, comps, (30, 50), teacher)  # 80
        _enter(st, english, t3, comps, (25, 35), teacher)  # 60
        _submit([maths, english], t3)
        compute_class(arm, t3)

        m = SubjectResult.objects.get(student=st, offering=maths, term=t3)
        e = SubjectResult.objects.get(student=st, offering=english, term=t3)
        assert (m.total, m.cumulative) == (D("80"), D("70.00"))
        assert (e.total, e.cumulative) == (D("60"), D("55.00"))
        assert str(m.cumulative) == "70.00" and str(e.cumulative) == "55.00"
        s = ResultSummary.objects.get(student=st, term=t3)
        assert s.cumulative_average == D("62.50")
        assert (s.position, s.out_of, s.average) == (1, 1, D("70.00"))
        # The 3rd-term class result workflow is untouched by computing.
        assert not ClassResult.objects.filter(arm=arm, term=t3).exclude(status="draft").exists()


def test_cumulative_prefers_computed_earlier_result(school_a):
    """An earlier term's SubjectResult wins over its raw scores."""
    with schema_context("school_a"):
        Term.objects.filter(is_current=True).update(is_current=False)
        session = default_session()
        t1, t3 = _term(session, 1), _term(session, 3, current=True)
        level, _ = ClassLevel.objects.get_or_create(
            name="JSS 3", defaults={"section": "jss", "order": 11}
        )
        arm = ClassArm.objects.create(level=level, suffix="D")
        comps = list(_jss_structure())
        teacher = StaffFactory()
        maths = _offerings(arm, session, teacher, ["Mathematics"])["Mathematics"]
        st = StudentProfileFactory(enrolment__arm=arm, enrolment__session=session)
        _enter(st, maths, t1, comps, (20, 40), teacher)  # 60
        _submit([maths], t1)
        compute_class(arm, t1)
        # The 1st-term result stands at 60 even if the raw scores later differ.
        Score.objects.filter(term=t1).update(value=D("0"))
        _enter(st, maths, t3, comps, (30, 50), teacher)  # 80
        _submit([maths], t3)
        compute_class(arm, t3)
        assert SubjectResult.objects.get(term=t3).cumulative == D("70.00")
