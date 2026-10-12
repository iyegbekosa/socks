from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, ClassArm, ClassLevel, SubjectOffering, Term
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results import scope
from apps.results.models import AssessmentComponent, ClassResult, ElectiveChoice, Score, ScoreSheet
from apps.results.scores import (
    CellInput,
    ResultsLocked,
    VersionConflict,
    parse_score,
    save_cells,
    sheet_rows,
)
from apps.results.services import save_components, set_electives
from apps.students.models import Enrolment
from tests.accounts.factories import StaffFactory
from tests.results.factories import make_sheet_world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield make_sheet_world()


def cell(student, comp, value, version=0):
    return CellInput(student_id=student.pk, component_id=comp.pk, value=value, version=version)


def stored(w, student, comp):
    return Score.objects.filter(
        offering=w.offering, term=w.term, student=student, component=comp
    ).first()


# --- reading -------------------------------------------------------------------------------


def test_sheet_rows_lists_takers_with_blank_cells(w):
    data = sheet_rows(w.offering, w.term)
    assert [r["student_id"] for r in data.rows] == [s.pk for s in scope.students_taking(w.offering)]
    assert {r["student_id"] for r in data.rows} == {s.pk for s in w.students}
    assert [c.pk for c in data.components] == [c.pk for c in w.comps]
    for row in data.rows:
        assert [(c["component_id"], c["value"], c["version"]) for c in row["cells"]] == [
            (c.pk, None, 0) for c in w.comps
        ]
    assert data.sheet_status == "draft" and data.class_status == "draft" and data.editable
    assert ScoreSheet.objects.filter(offering=w.offering, term=w.term, status="draft").exists()
    assert ClassResult.objects.filter(arm=w.arm, term=w.term, status="draft").exists()


def test_sheet_rows_term_must_be_in_offering_session(w):
    other = AcademicSession.objects.create(
        name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
    )
    t = Term.objects.create(
        session=other, number=1, start_date=date(2027, 9, 1), end_date=date(2027, 12, 1)
    )
    with pytest.raises(ValidationError):
        sheet_rows(w.offering, t)
    with pytest.raises(ValidationError):
        save_cells(w.offering, t, [cell(w.students[0], w.comps[0], "5")], by=w.teacher)
    assert not Score.objects.exists()


# --- saving --------------------------------------------------------------------------------


def test_save_three_cells_gives_version_one(w):
    s = w.students
    result = save_cells(
        w.offering,
        w.term,
        [cell(s[0], w.comps[0], "7.5"), cell(s[1], w.comps[0], "10"), cell(s[2], w.comps[4], "0")],
        by=w.teacher,
    )
    assert sorted((v["student"], v["component"], v["version"]) for v in result.versions) == sorted(
        [(s[0].pk, w.comps[0].pk, 1), (s[1].pk, w.comps[0].pk, 1), (s[2].pk, w.comps[4].pk, 1)]
    )
    assert result.changed == 3
    row = stored(w, s[0], w.comps[0])
    assert row.value == Decimal("7.5") and row.version == 1 and row.entered_by == w.teacher
    assert stored(w, s[2], w.comps[4]).value == Decimal("0")
    data = sheet_rows(w.offering, w.term)
    first = next(r for r in data.rows if r["student_id"] == s[0].pk)
    assert first["cells"][0] == {"component_id": w.comps[0].pk, "value": "7.5", "version": 1}


def test_second_writer_with_stale_version_conflicts_and_saves_nothing(w):
    s = w.students
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "5")], by=w.teacher)
    # Both clients read version 1. The first save wins.
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "6", 1)], by=w.teacher)
    with pytest.raises(VersionConflict) as exc:
        save_cells(
            w.offering,
            w.term,
            [cell(s[0], w.comps[0], "9", 1), cell(s[1], w.comps[1], "4")],
            by=w.teacher,
        )
    assert exc.value.conflicts == [
        {"student": s[0].pk, "component": w.comps[0].pk, "value": "6.0", "version": 2}
    ]
    assert stored(w, s[0], w.comps[0]).value == Decimal("6")
    assert stored(w, s[1], w.comps[1]) is None


def test_creating_an_existing_cell_with_version_zero_conflicts(w):
    s = w.students
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "5")], by=w.teacher)
    with pytest.raises(VersionConflict) as exc:
        save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "8")], by=w.teacher)
    assert exc.value.conflicts[0]["version"] == 1
    assert stored(w, s[0], w.comps[0]).value == Decimal("5")


def test_conflict_on_missing_cell_reports_null_and_zero(w):
    with pytest.raises(VersionConflict) as exc:
        save_cells(w.offering, w.term, [cell(w.students[0], w.comps[0], "5", 3)], by=w.teacher)
    assert exc.value.conflicts[0]["value"] is None
    assert exc.value.conflicts[0]["version"] == 0


def test_bad_cells_refused_per_cell_and_nothing_saved(w):
    s = w.students
    with pytest.raises(ValidationError) as exc:
        save_cells(
            w.offering,
            w.term,
            [
                cell(s[0], w.comps[0], "11"),
                cell(s[0], w.comps[1], "-1"),
                cell(s[1], w.comps[0], "abc"),
                cell(s[1], w.comps[1], "7.25"),
                cell(s[2], w.comps[0], "8"),
            ],
            by=w.teacher,
        )
    keys = set(exc.value.message_dict)
    assert keys == {
        f"{s[0].pk}:{w.comps[0].pk}",
        f"{s[0].pk}:{w.comps[1].pk}",
        f"{s[1].pk}:{w.comps[0].pk}",
        f"{s[1].pk}:{w.comps[1].pk}",
    }
    assert not Score.objects.exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", Decimal("0")),
        ("10", Decimal("10")),
        ("9.5", Decimal("9.5")),
        (" 7.5 ", Decimal("7.5")),
        ("100", Decimal("100")),
        ("", None),
        (None, None),
    ],
)
def test_parse_score_accepts(raw, expected):
    assert parse_score(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["7.25", "1e1", "NaN", "-0", " ", "+1", "1.", ".5", "1000", "٣", "Infinity", "1,5"],
)
def test_parse_score_rejects(raw):
    with pytest.raises(ValueError):
        parse_score(raw)


def test_value_at_max_ok_and_above_max_refused(w):
    s = w.students
    save_cells(w.offering, w.term, [cell(s[0], w.comps[4], "60")], by=w.teacher)
    with pytest.raises(ValidationError):
        save_cells(w.offering, w.term, [cell(s[1], w.comps[4], "60.1")], by=w.teacher)


def test_duplicate_cell_in_one_request_refused(w):
    s = w.students
    with pytest.raises(ValidationError):
        save_cells(
            w.offering,
            w.term,
            [cell(s[0], w.comps[0], "5"), cell(s[0], w.comps[0], "6")],
            by=w.teacher,
        )
    assert not Score.objects.exists()


def test_clearing_keeps_row_and_bumps_version(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    result = save_cells(w.offering, w.term, [cell(st, comp, "", 1)], by=w.teacher)
    row = stored(w, st, comp)
    assert row.value is None and row.version == 2
    assert result.versions == [{"student": st.pk, "component": comp.pk, "version": 2}]
    save_cells(w.offering, w.term, [cell(st, comp, None, 2)], by=w.teacher)
    assert stored(w, st, comp).version == 2  # already blank: unchanged, skipped


def test_unchanged_values_skipped_and_not_audited(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    ScoreSheet.objects.filter(offering=w.offering, term=w.term).update(
        first_submitted_at=timezone.now()
    )
    result = save_cells(w.offering, w.term, [cell(st, comp, "5.0", 1)], by=w.teacher)
    assert result.changed == 0
    assert result.versions == [{"student": st.pk, "component": comp.pk, "version": 1}]
    assert stored(w, st, comp).version == 1
    assert not AuditEvent.objects.filter(action="results.score_changed").exists()


def test_blank_missing_cell_with_version_zero_is_not_created(w):
    save_cells(w.offering, w.term, [cell(w.students[0], w.comps[0], None)], by=w.teacher)
    assert not Score.objects.exists()


def test_score_changed_audited_only_after_first_submission(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    assert not AuditEvent.objects.filter(action="results.score_changed").exists()
    ScoreSheet.objects.filter(offering=w.offering, term=w.term).update(
        first_submitted_at=timezone.now()
    )
    save_cells(w.offering, w.term, [cell(st, comp, "6.5", 1)], by=w.teacher)
    ev = AuditEvent.objects.get(action="results.score_changed")
    assert ev.before == {"student_id": st.pk, "component_id": comp.pk, "value": "5.0"}
    assert ev.after == {"student_id": st.pk, "component_id": comp.pk, "value": "6.5"}
    assert ev.actor_id == w.teacher.pk
    assert ev.object_type == "results.scoresheet"


# --- locks ---------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["sheet", "class"])
def test_locked_sheet_refused_and_audited(w, which):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    if which == "sheet":
        ScoreSheet.objects.filter(offering=w.offering, term=w.term).update(status="submitted")
    else:
        ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="published")
    with pytest.raises(ResultsLocked):
        save_cells(w.offering, w.term, [cell(st, comp, "9", 1)], by=w.teacher)
    assert stored(w, st, comp).value == Decimal("5")
    ev = AuditEvent.objects.get(action="results.edit_refused")
    assert ev.after == {"offering_id": w.offering.pk, "term_id": w.term.pk}
    assert ev.actor_id == w.teacher.pk
    assert sheet_rows(w.offering, w.term).editable is False


# --- who takes the subject -----------------------------------------------------------------


def test_late_enrolment_appears_blank_and_withdrawn_refused(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    late = StudentProfileFactory()
    data = sheet_rows(w.offering, w.term)
    late_row = next(r for r in data.rows if r["student_id"] == late.pk)
    assert all(c["value"] is None and c["version"] == 0 for c in late_row["cells"])
    assert w.withdrawn.pk not in {r["student_id"] for r in data.rows}
    with pytest.raises(ValidationError) as exc:
        save_cells(w.offering, w.term, [cell(w.withdrawn, comp, "5")], by=w.teacher)
    assert set(exc.value.message_dict) == {f"{w.withdrawn.pk}:{comp.pk}"}
    assert not Score.objects.filter(student=w.withdrawn).exists()


def test_student_withdrawn_after_scores_is_unlisted_and_refused(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    Enrolment.objects.filter(student=st).update(status=Enrolment.Status.WITHDRAWN)
    assert st.pk not in {r["student_id"] for r in sheet_rows(w.offering, w.term).rows}
    with pytest.raises(ValidationError):
        save_cells(w.offering, w.term, [cell(st, comp, "6", 1)], by=w.teacher)
    assert stored(w, st, comp).value == Decimal("5")


def test_elective_only_for_chosen_students(w):
    w.offering.is_compulsory = False
    w.offering.save()
    s = w.students
    ElectiveChoice.objects.create(student=s[0], offering=w.offering)
    assert [r["student_id"] for r in sheet_rows(w.offering, w.term).rows] == [s[0].pk]
    with pytest.raises(ValidationError):
        save_cells(w.offering, w.term, [cell(s[1], w.comps[0], "5")], by=w.teacher)


def test_component_from_other_section_refused(w):
    sss = AssessmentComponent.objects.create(section="sss", name="Zz", max_score=10, order=9)
    with pytest.raises(ValidationError) as exc:
        save_cells(w.offering, w.term, [cell(w.students[0], sss, "5")], by=w.teacher)
    assert set(exc.value.message_dict) == {f"{w.students[0].pk}:{sss.pk}"}


# --- permission ----------------------------------------------------------------------------


def test_can_enter_scores(w):
    assert scope.can_enter_scores(w.teacher, w.offering)
    other = StaffFactory()
    set_roles(other, ["teacher"], by=None)
    assert not scope.can_enter_scores(other, w.offering)
    principal = StaffFactory()
    set_roles(principal, ["principal", "school_admin"], by=None)
    assert not scope.can_enter_scores(principal, w.offering)
    assert not scope.can_enter_scores(w.students[0].user, w.offering)
    set_roles(w.teacher, [], by=None)
    assert not scope.can_enter_scores(w.teacher, w.offering)
    set_roles(w.teacher, ["teacher"], by=None)
    w.teacher.is_active = False
    w.teacher.save()
    assert not scope.can_enter_scores(w.teacher, w.offering)


# --- hooks into Tasks 2 and 3 --------------------------------------------------------------


def test_has_scores_real(w):
    assert not scope.has_scores("jss")
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, None)], by=w.teacher)
    assert not scope.has_scores("jss")
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    assert scope.has_scores("jss")
    assert not scope.has_scores("sss")
    save_cells(w.offering, w.term, [cell(st, comp, "", 1)], by=w.teacher)
    assert not scope.has_scores("jss")  # cleared cells do not count


def test_has_scores_ignores_other_sessions(w):
    save_cells(w.offering, w.term, [cell(w.students[0], w.comps[0], "5")], by=w.teacher)
    nxt = AcademicSession.objects.create(
        name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
    )
    Term.objects.filter(is_current=True).update(is_current=False)
    Term.objects.create(
        session=nxt,
        number=1,
        start_date=date(2027, 9, 1),
        end_date=date(2027, 12, 1),
        is_current=True,
    )
    assert not scope.has_scores("jss")


def test_has_student_scores(w):
    st, comp = w.students[0], w.comps[0]
    assert not scope.has_student_scores(w.offering, st)
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    assert scope.has_student_scores(w.offering, st)
    assert not scope.has_student_scores(w.offering, w.students[1])


def test_components_refused_once_scores_exist(w):
    save_cells(w.offering, w.term, [cell(w.students[0], w.comps[0], "5")], by=w.teacher)
    with pytest.raises(ValidationError):
        save_components("jss", [{"name": "Exam", "max_score": 100, "order": 1}], by=w.teacher)
    assert AssessmentComponent.objects.filter(section="jss").count() == 5


def test_resubmitted_component_keeps_pk(w):
    exam = AssessmentComponent.objects.get(section="jss", name="Exam")
    ca1 = AssessmentComponent.objects.get(section="jss", name="CA1")
    save_components(
        "jss",
        [
            {"name": "ca1", "max_score": 30, "order": 1},
            {"name": "Exam", "max_score": 70, "order": 2},
        ],
        by=w.teacher,
    )
    rows = list(AssessmentComponent.objects.filter(section="jss"))
    assert [(c.pk, c.name, c.max_score, c.order) for c in rows] == [
        (ca1.pk, "ca1", 30, 1),
        (exam.pk, "Exam", 70, 2),
    ]


def test_removed_component_with_blank_cells_can_go(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    save_cells(w.offering, w.term, [cell(st, comp, "", 1)], by=w.teacher)
    save_components("jss", [{"name": "Exam", "max_score": 100, "order": 1}], by=w.teacher)
    assert [c.name for c in AssessmentComponent.objects.filter(section="jss")] == ["Exam"]
    assert not Score.objects.exists()


def test_removed_component_with_old_session_scores_refused(w):
    st, comp = w.students[0], w.comps[0]
    save_cells(w.offering, w.term, [cell(st, comp, "5")], by=w.teacher)
    nxt = AcademicSession.objects.create(
        name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
    )
    Term.objects.filter(is_current=True).update(is_current=False)
    Term.objects.create(
        session=nxt,
        number=1,
        start_date=date(2027, 9, 1),
        end_date=date(2027, 12, 1),
        is_current=True,
    )
    with pytest.raises(ValidationError):
        save_components("jss", [{"name": "Exam", "max_score": 100, "order": 1}], by=w.teacher)
    assert AssessmentComponent.objects.filter(section="jss").count() == 5


def _elective(w):
    w.offering.is_compulsory = False
    w.offering.save()
    s = w.students
    set_electives(w.offering, [s[0].pk, s[1].pk], by=w.teacher)
    return s


def test_elective_removal_refused_for_active_student_with_scores(w):
    s = _elective(w)
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "5")], by=w.teacher)
    with pytest.raises(ValidationError) as exc:
        set_electives(w.offering, [s[1].pk], by=w.teacher)
    message = " ".join(exc.value.messages)
    assert "1 student" in message
    assert s[0].first_name not in message and s[0].last_name not in message
    assert ElectiveChoice.objects.filter(offering=w.offering).count() == 2
    set_electives(w.offering, [s[0].pk], by=w.teacher)  # removing one without scores is fine
    assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[0].pk}


def test_elective_removal_allowed_for_blank_cells_only(w):
    s = _elective(w)
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "5")], by=w.teacher)
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "", 1)], by=w.teacher)
    set_electives(w.offering, [s[1].pk], by=w.teacher)
    assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[1].pk}


@pytest.mark.parametrize("how", ["moved", "withdrawn"])
def test_stale_elective_with_scores_removable_and_scores_kept(w, how):
    s = _elective(w)
    save_cells(w.offering, w.term, [cell(s[0], w.comps[0], "5")], by=w.teacher)
    if how == "moved":
        other = ClassArm.objects.create(level=w.arm.level, suffix="B")
        Enrolment.objects.filter(student=s[0]).update(arm=other)
    else:
        s[0].status = "withdrawn"
        s[0].save()
    set_electives(w.offering, [s[1].pk], by=w.teacher)
    assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[1].pk}
    assert Score.objects.filter(student=s[0], value=Decimal("5")).exists()


def test_score_fks_protect(w):
    save_cells(w.offering, w.term, [cell(w.students[0], w.comps[0], "5")], by=w.teacher)
    from django.db.models import ProtectedError

    for obj in (w.comps[0], w.offering, w.term):
        with pytest.raises(ProtectedError):
            obj.delete()
    assert SubjectOffering.objects.filter(pk=w.offering.pk).exists()
    assert ClassLevel.objects.exists()
