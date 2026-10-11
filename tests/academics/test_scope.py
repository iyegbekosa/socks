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
