from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, FormTeacher, Subject, SubjectOffering, Term
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results.models import ElectiveChoice
from apps.results.scope import students_taking
from apps.results.services import set_electives
from apps.students.models import Enrolment
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.students.factories import StudentProfileFactory, default_arm, default_session

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(school_a):
    with schema_context("school_a"):
        session = default_session()
        arm = default_arm()
        other_arm = ClassArm.objects.create(level=arm.level, suffix="B")
        subj_c = Subject.objects.create(name="Maths", code="MTH", sections=["jss"])
        subj_e = Subject.objects.create(name="French", code="FRN", sections=["jss"])
        compulsory = SubjectOffering.objects.create(
            subject=subj_c, arm=arm, session=session, is_compulsory=True
        )
        elective = SubjectOffering.objects.create(
            subject=subj_e, arm=arm, session=session, is_compulsory=False
        )
        students = [StudentProfileFactory() for _ in range(3)]
        withdrawn = StudentProfileFactory(enrolment__status=Enrolment.Status.WITHDRAWN)
        outsider = StudentProfileFactory(enrolment__arm=other_arm)
        admin = StaffFactory()
        set_roles(admin, ["school_admin"], by=admin)
        ft = StaffFactory()
        set_roles(ft, ["teacher"], by=ft)
        FormTeacher.objects.create(arm=arm, session=session, teacher=ft)
        other = StaffFactory()
        set_roles(other, ["teacher"], by=other)
        yield {
            "compulsory": compulsory,
            "elective": elective,
            "students": students,
            "withdrawn": withdrawn,
            "outsider": outsider,
            "admin": admin,
            "ft": ft,
            "other": other,
        }


def test_compulsory_covers_active_students_only(world):
    with schema_context("school_a"):
        got = set(students_taking(world["compulsory"]).values_list("pk", flat=True))
        assert got == {s.pk for s in world["students"]}
        assert world["withdrawn"].pk not in got


def test_elective_covers_only_chosen(world):
    s = world["students"]
    with schema_context("school_a"):
        set_electives(world["elective"], [s[0].pk, s[1].pk], by=world["admin"])
        assert {x.pk for x in students_taking(world["elective"])} == {s[0].pk, s[1].pk}


def test_withdrawn_with_choice_excluded(world):
    with schema_context("school_a"):
        w = world["withdrawn"]
        ElectiveChoice.objects.bulk_create([ElectiveChoice(student=w, offering=world["elective"])])
        assert w.pk not in {x.pk for x in students_taking(world["elective"])}


def test_student_in_other_arm_rejected(world):
    with schema_context("school_a"):
        with pytest.raises(ValidationError):
            set_electives(
                world["elective"],
                [world["students"][0].pk, world["outsider"].pk],
                by=world["admin"],
            )
        assert ElectiveChoice.objects.count() == 0


def test_compulsory_offering_rejected(world):
    with schema_context("school_a"):
        with pytest.raises(ValidationError):
            set_electives(world["compulsory"], [world["students"][0].pk], by=world["admin"])
        with pytest.raises(ValidationError):
            ElectiveChoice(student=world["students"][0], offering=world["compulsory"]).full_clean()


def test_replace_set_audits_ids(world):
    s = world["students"]
    with schema_context("school_a"):
        set_electives(world["elective"], [s[0].pk, s[1].pk], by=world["admin"])
        set_electives(world["elective"], [s[1].pk, s[2].pk], by=world["admin"])
        assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[1].pk, s[2].pk}
        ev = AuditEvent.objects.filter(action="results.elective_changed").latest("pk")
        assert ev.after == {
            "offering_id": world["elective"].pk,
            "added": [s[2].pk],
            "removed": [s[0].pk],
        }


def test_non_form_teacher_forbidden(client_a, world):
    with schema_context("school_a"):
        client_a.force_login(world["other"])
    url = reverse("results:electives", args=[world["elective"].pk])
    assert client_a.get(url).status_code == 403
    assert client_a.post(url, {"students": []}).status_code == 403


def test_unknown_offering_404(client_a, world):
    login_verified(client_a, world["admin"])
    assert client_a.get(reverse("results:electives", args=[999999])).status_code == 404


def test_compulsory_page_has_no_form(client_a, world):
    login_verified(client_a, world["admin"])
    r = client_a.get(reverse("results:electives", args=[world["compulsory"].pk]))
    assert r.status_code == 200
    assert "every student in the class takes it" in r.content.decode()
    assert 'name="students"' not in r.content.decode()


def test_form_teacher_can_save(client_a, world):
    s = world["students"]
    with schema_context("school_a"):
        client_a.force_login(world["ft"])
    url = reverse("results:electives", args=[world["elective"].pk])
    assert client_a.get(url).status_code == 200
    r = client_a.post(url, {"students": [s[0].pk, s[2].pk]})
    assert r.status_code == 302
    with schema_context("school_a"):
        assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[0].pk, s[2].pk}
        assert AuditEvent.objects.filter(action="results.elective_changed").exists()
    r = client_a.get(url)
    body = r.content.decode()
    for i, st in enumerate(s):
        tag = f'value="{st.pk}" checked>'
        assert (tag in body) == (i != 1)


def test_bad_post_saves_nothing(client_a, world):
    login_verified(client_a, world["admin"])
    url = reverse("results:electives", args=[world["elective"].pk])
    r = client_a.post(url, {"students": [world["students"][0].pk, world["outsider"].pk, "x"]})
    assert r.status_code == 200
    with schema_context("school_a"):
        assert ElectiveChoice.objects.count() == 0


def test_arm_detail_links_only_elective(client_a, world):
    login_verified(client_a, world["admin"])
    with schema_context("school_a"):
        arm_id = world["elective"].arm_id
        sess = default_session()
        Term.objects.create(
            session=sess,
            number=1,
            start_date=sess.start_date,
            end_date=date(2026, 12, 15),
            is_current=True,
        )
    r = client_a.get(reverse("academics:arm_detail", args=[arm_id]))
    body = r.content.decode()
    assert reverse("results:electives", args=[world["elective"].pk]) in body
    assert reverse("results:electives", args=[world["compulsory"].pk]) not in body


def test_non_ascii_and_huge_ids_rejected(client_a, world):
    login_verified(client_a, world["admin"])
    url = reverse("results:electives", args=[world["elective"].pk])
    for bad in ("\u00b2", "9" * 5000):
        r = client_a.post(url, {"students": [world["students"][0].pk, bad]})
        assert r.status_code == 200
        assert "Choose only students from the list" in r.content.decode()
    with schema_context("school_a"):
        assert ElectiveChoice.objects.count() == 0


def test_moved_student_choice_removed_and_audited(world):
    s = world["students"]
    with schema_context("school_a"):
        set_electives(world["elective"], [s[0].pk, s[1].pk], by=world["admin"])
        other_arm = ClassArm.objects.get(suffix="B")
        Enrolment.objects.filter(student=s[0]).update(arm=other_arm)
        assert s[0].pk not in {x.pk for x in students_taking(world["elective"])}
        set_electives(world["elective"], [s[1].pk], by=world["admin"])
        assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[1].pk}
        ev = AuditEvent.objects.filter(action="results.elective_changed").latest("pk")
        assert ev.after["removed"] == [s[0].pk]


def test_status_withdrawn_student_excluded(world):
    with schema_context("school_a"):
        st = world["students"][0]
        ElectiveChoice.objects.create(student=st, offering=world["elective"])
        st.status = "withdrawn"
        st.save()
        assert st.pk not in {x.pk for x in students_taking(world["elective"])}


def test_form_teacher_of_other_arm_forbidden(client_a, world):
    with schema_context("school_a"):
        other_arm = ClassArm.objects.get(suffix="B")
        FormTeacher.objects.filter(teacher=world["ft"]).update(arm=other_arm)
        client_a.force_login(world["ft"])
    url = reverse("results:electives", args=[world["elective"].pk])
    assert client_a.get(url).status_code == 403


def test_form_teacher_of_other_session_forbidden(client_a, world):
    with schema_context("school_a"):
        from apps.academics.models import AcademicSession

        sess = AcademicSession.objects.create(
            name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
        )
        FormTeacher.objects.filter(teacher=world["ft"]).update(session=sess)
        client_a.force_login(world["ft"])
    url = reverse("results:electives", args=[world["elective"].pk])
    assert client_a.get(url).status_code == 403


def _current_term(session):
    from datetime import date

    from apps.academics.models import Term

    Term.objects.filter(is_current=True).update(is_current=False)
    term, _ = Term.objects.update_or_create(
        session=session,
        number=1,
        defaults={
            "start_date": date(2026, 9, 1),
            "end_date": date(2026, 12, 15),
            "is_current": True,
        },
    )
    return term


@pytest.mark.parametrize("status", ["reviewed", "approved", "published"])
def test_electives_refused_once_the_class_moved_past_draft(client_a, world, status):
    from apps.academics.services import current_term
    from apps.results.models import ClassResult
    from apps.results.workflow import ResultsStateError

    s = world["students"]
    with schema_context("school_a"):
        set_electives(world["elective"], [s[0].pk], by=world["admin"])
        _current_term(world["elective"].session)
        term = current_term()
        assert term is not None and term.session_id == world["elective"].session_id
        ClassResult.objects.create(arm=world["elective"].arm, term=term, status=status)
        with pytest.raises(ResultsStateError, match="past draft"):
            set_electives(world["elective"], [s[0].pk, s[1].pk], by=world["admin"])
        client_a.force_login(world["ft"])
    r = client_a.post(
        reverse("results:electives", args=[world["elective"].pk]), {"students": [s[1].pk]}
    )
    assert r.status_code == 409 and "past draft" in r.content.decode()
    with schema_context("school_a"):
        assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[0].pk}
        assert AuditEvent.objects.filter(action="results.elective_changed").count() == 1


def test_electives_change_while_the_class_is_draft(world):
    from apps.results.models import ClassResult

    s = world["students"]
    with schema_context("school_a"):
        term = _current_term(world["elective"].session)
        ClassResult.objects.create(arm=world["elective"].arm, term=term)
        set_electives(world["elective"], [s[2].pk], by=world["admin"])
        assert {c.student_id for c in ElectiveChoice.objects.all()} == {s[2].pk}


def test_back_link_opens_for_each_role(client_a, world):
    """A form teacher's Back goes to their sheets (arm_detail is admin/principal only)."""
    url = reverse("results:electives", args=[world["elective"].pk])
    arm_page = reverse("academics:arm_detail", args=[world["elective"].arm_id])
    with schema_context("school_a"):
        client_a.force_login(world["ft"])
    html = client_a.get(url).content.decode()
    assert f'href="{reverse("results:my_sheets")}">Back' in html and arm_page not in html

    login_verified(client_a, world["admin"])
    assert f'href="{arm_page}">Back' in client_a.get(url).content.decode()
