from datetime import date, timedelta

import pytest
from django_tenants.utils import schema_context

from apps.academics.models import (
    AcademicSession,
    ClassArm,
    ClassLevel,
    FormTeacher,
    Subject,
    SubjectOffering,
)
from apps.academics.scope import arms_taught_by
from apps.accounts.roles import set_roles
from tests.accounts.factories import StaffFactory, StudentFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(school_a):
    with schema_context("school_a"):
        today = date.today()
        s1 = AcademicSession.objects.create(
            name="2025/2026",
            start_date=today - timedelta(days=400),
            end_date=today - timedelta(days=40),
        )
        s2 = AcademicSession.objects.create(
            name="2026/2027",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=300),
        )
        lvl = ClassLevel.objects.create(name="JSS 1", section="jss", order=1)
        arms = [ClassArm.objects.create(level=lvl, suffix=x) for x in "ABCD"]
        subj = Subject.objects.create(name="Maths", code="maths", sections=["jss"])
        t = StaffFactory()
        set_roles(t, ["teacher", "school_admin"], by=t)
        yield {"s1": s1, "s2": s2, "arms": arms, "subj": subj, "t": t}
        FormTeacher.objects.all().delete()
        SubjectOffering.objects.all().delete()
        AcademicSession.objects.all().delete()
        ClassArm.objects.all().delete()
        ClassLevel.objects.all().delete()
        Subject.objects.all().delete()


def test_form_arm_plus_offering_arms_current_session_only(world):
    a, b, c, d = world["arms"]
    t, subj = world["t"], world["subj"]
    with schema_context("school_a"):
        FormTeacher.objects.create(arm=a, session=world["s2"], teacher=t)
        FormTeacher.objects.create(arm=d, session=world["s1"], teacher=t)
        SubjectOffering.objects.create(subject=subj, arm=b, session=world["s2"], teacher=t)
        SubjectOffering.objects.create(subject=subj, arm=a, session=world["s2"], teacher=t)
        SubjectOffering.objects.create(subject=subj, arm=c, session=world["s1"], teacher=t)
        got = list(arms_taught_by(t, world["s2"]))
        assert sorted(x.pk for x in got) == sorted([a.pk, b.pk])  # distinct, no c or d


def test_none_session_student_and_inactive_get_nothing(world):
    with schema_context("school_a"):
        t = world["t"]
        FormTeacher.objects.create(arm=world["arms"][0], session=world["s2"], teacher=t)
        assert not arms_taught_by(t, None).exists()
        assert not arms_taught_by(StudentFactory(), world["s2"]).exists()
        t.is_active = False
        t.save()
        assert not arms_taught_by(t, world["s2"]).exists()


def test_losing_teacher_role_empties_scope(world):
    from apps.accounts.roles import set_roles as _set

    with schema_context("school_a"):
        t = world["t"]
        FormTeacher.objects.create(arm=world["arms"][0], session=world["s2"], teacher=t)
        assert arms_taught_by(t, world["s2"]).exists()
        _set(t, ["school_admin"], by=t)
        assert FormTeacher.objects.filter(teacher=t).exists()
        assert not arms_taught_by(t, world["s2"]).exists()


def test_students_visible_to(world):
    from apps.academics.models import Term
    from apps.academics.scope import students_visible_to
    from apps.students.models import Enrolment, Student
    from apps.students.services import withdraw_student
    from tests.students.factories import StudentProfileFactory

    a, b, c = world["arms"][:3]
    with schema_context("school_a"):
        Term.objects.create(
            session=world["s2"],
            number=1,
            start_date=world["s2"].start_date,
            end_date=world["s2"].end_date,
            is_current=True,
        )
        t = world["t"]
        FormTeacher.objects.create(arm=a, session=world["s2"], teacher=t)
        SubjectOffering.objects.create(subject=world["subj"], arm=a, session=world["s2"], teacher=t)
        mine = StudentProfileFactory(enrolment__arm=a, enrolment__session=world["s2"])
        gone = StudentProfileFactory(enrolment__arm=a, enrolment__session=world["s2"])
        other = StudentProfileFactory(enrolment__arm=b, enrolment__session=world["s2"])
        old = StudentProfileFactory(enrolment__arm=a, enrolment__session=world["s1"])
        withdraw_student(gone, by=None)
        # school_admin + teacher: admin wins, sees everyone
        assert set(students_visible_to(t)) == set(Student.objects.all())
        teacher_only = StaffFactory()
        set_roles(teacher_only, ["teacher"], by=t)
        subj2 = Subject.objects.create(name="English", code="eng", sections=["jss"])
        SubjectOffering.objects.create(
            subject=subj2, arm=a, session=world["s2"], teacher=teacher_only
        )
        SubjectOffering.objects.create(
            subject=world["subj"], arm=c, session=world["s2"], teacher=teacher_only
        )
        FormTeacher.objects.create(arm=c, session=world["s2"], teacher=teacher_only)
        qs = students_visible_to(teacher_only)
        assert list(qs) == [mine]  # distinct; no other arm, old session or withdrawn
        assert not students_visible_to(StudentFactory()).exists()
        assert not students_visible_to(None).exists()
        for role in ("bursar", "principal", "proprietor"):
            u = StaffFactory()
            set_roles(u, [role], by=t)
            assert students_visible_to(u).count() == Student.objects.count()
        assert other in students_visible_to(t) and old in students_visible_to(t)
        Enrolment.objects.all().delete()
        Student.objects.all().delete()
