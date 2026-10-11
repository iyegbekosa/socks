from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import (
    AcademicSession,
    ClassArm,
    ClassLevel,
    FormTeacher,
    Subject,
    SubjectOffering,
    Term,
)
from apps.academics.services import assign_form_teacher, assign_offering, delete_subject
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db


def wipe():
    with schema_context("school_a"):
        FormTeacher.objects.all().delete()
        SubjectOffering.objects.all().delete()
        Term.objects.all().delete()
        AcademicSession.objects.all().delete()
        ClassArm.objects.all().delete()
        ClassLevel.objects.all().delete()
        Subject.objects.all().delete()


@pytest.fixture(autouse=True)
def _clean(school_a):
    wipe()
    yield
    wipe()


def make_staff(*roles):
    u = StaffFactory()
    set_roles(u, list(roles), by=u)
    return u


@pytest.fixture
def world(school_a):
    with schema_context("school_a"):
        today = date.today()
        current = AcademicSession.objects.create(
            name="2026/2027",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=300),
        )
        Term.objects.create(
            session=current,
            number=1,
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=60),
            is_current=True,
        )
        past = AcademicSession.objects.create(
            name="2024/2025",
            start_date=today - timedelta(days=800),
            end_date=today - timedelta(days=500),
        )
        future = AcademicSession.objects.create(
            name="2027/2028",
            start_date=today + timedelta(days=400),
            end_date=today + timedelta(days=700),
        )
        jss = ClassLevel.objects.create(name="JSS 1", section="jss", order=1)
        sss = ClassLevel.objects.create(name="SSS 1", section="sss", order=1)
        return {
            "current": current,
            "past": past,
            "future": future,
            "jss_arm": ClassArm.objects.create(level=jss, suffix="A"),
            "sss_arm": ClassArm.objects.create(level=sss, suffix="A"),
            "maths": Subject.objects.create(name="Maths", code="maths", sections=["jss", "sss"]),
            "basic": Subject.objects.create(name="Basic Sci", code="basic", sections=["jss"]),
            "teacher": make_staff("teacher"),
            "teacher2": make_staff("teacher"),
        }


def audits(action):
    return list(AuditEvent.objects.filter(action=action).order_by("id"))


def test_wrong_section_refused(world):
    with schema_context("school_a"):
        with pytest.raises(ValidationError):
            assign_offering(
                subject=world["basic"],
                arm=world["sss_arm"],
                session=world["current"],
                teacher=world["teacher"],
                by=world["teacher"],
            )
        assert SubjectOffering.objects.count() == 0


def test_non_teacher_inactive_and_student_refused(world):
    with schema_context("school_a"):
        admin = make_staff("school_admin")
        inactive = make_staff("teacher")
        inactive.is_active = False
        inactive.save()
        student = StudentFactory()
        for bad in (admin, inactive, student):
            with pytest.raises(ValidationError):
                assign_offering(
                    subject=world["maths"],
                    arm=world["jss_arm"],
                    session=world["current"],
                    teacher=bad,
                    by=admin,
                )
            with pytest.raises(ValidationError):
                assign_form_teacher(
                    arm=world["jss_arm"], session=world["current"], teacher=bad, by=admin
                )


def test_ended_session_refused_future_allowed(world):
    with schema_context("school_a"):
        t = world["teacher"]
        with pytest.raises(ValidationError):
            assign_offering(
                subject=world["maths"],
                arm=world["jss_arm"],
                session=world["past"],
                teacher=t,
                by=t,
            )
        with pytest.raises(ValidationError):
            assign_form_teacher(arm=world["jss_arm"], session=world["past"], teacher=t, by=t)
        assign_offering(
            subject=world["maths"],
            arm=world["jss_arm"],
            session=world["future"],
            teacher=t,
            by=t,
        )
        assign_form_teacher(arm=world["jss_arm"], session=world["future"], teacher=t, by=t)


def test_reassign_replaces_and_audits(world):
    with schema_context("school_a"):
        kw = {"subject": world["maths"], "arm": world["jss_arm"], "session": world["current"]}
        o1 = assign_offering(**kw, teacher=world["teacher"], by=world["teacher"])
        o2 = assign_offering(
            **kw, teacher=world["teacher2"], is_compulsory=False, by=world["teacher"]
        )
        assert o1.pk == o2.pk
        assert SubjectOffering.objects.count() == 1
        o2.refresh_from_db()
        assert o2.teacher == world["teacher2"] and o2.is_compulsory is False
        f1 = assign_form_teacher(
            arm=world["jss_arm"], session=world["current"], teacher=world["teacher"], by=o2.teacher
        )
        f2 = assign_form_teacher(
            arm=world["jss_arm"], session=world["current"], teacher=world["teacher2"], by=o2.teacher
        )
        assert f1.pk == f2.pk and FormTeacher.objects.get().teacher == world["teacher2"]
        assert len(audits("academics.offering_assigned")) == 2
        ev = audits("academics.form_teacher_assigned")
        assert len(ev) == 2
        assert ev[1].after["teacher_id"] == world["teacher2"].pk
        assert ev[1].after["what"] == "form_teacher"


def test_unassigned_offering_allowed(world):
    with schema_context("school_a"):
        o = assign_offering(
            subject=world["maths"],
            arm=world["jss_arm"],
            session=world["current"],
            teacher=None,
            by=world["teacher"],
        )
        assert o.teacher is None


def test_delete_subject_with_offering_refused_friendly(world):
    with schema_context("school_a"):
        assign_offering(
            subject=world["maths"],
            arm=world["jss_arm"],
            session=world["current"],
            teacher=world["teacher"],
            by=world["teacher"],
        )
        with pytest.raises(ValidationError) as exc:
            delete_subject(world["maths"], by=world["teacher"])
        assert "offering" in str(exc.value).lower()
        assert Subject.objects.filter(pk=world["maths"].pk).exists()


# --- arm detail page ---------------------------------------------------------


def url(arm):
    return reverse("academics:arm_detail", args=[arm.pk])


def test_teacher_cannot_open_arm_detail(world, client_a):
    with schema_context("school_a"):
        client_a.force_login(world["teacher"])
    assert client_a.get(url(world["jss_arm"])).status_code == 403
    assert client_a.post(url(world["jss_arm"]), {}).status_code == 403


def test_principal_reads_but_cannot_post(world, client_a):
    with schema_context("school_a"):
        principal = make_staff("principal")
        login_verified(client_a, principal)
    assert client_a.get(url(world["jss_arm"])).status_code == 200
    assert client_a.post(url(world["jss_arm"]), {}).status_code == 403


def test_admin_saves_assignments_and_audits_changes_only(world, client_a):
    with schema_context("school_a"):
        admin = make_staff("school_admin")
        login_verified(client_a, admin)
    arm = world["jss_arm"]
    resp = client_a.get(url(arm))
    assert resp.status_code == 200
    assert "Maths" in resp.content.decode() and "Basic Sci" in resp.content.decode()
    t, t2 = world["teacher"], world["teacher2"]
    data = {
        "form_teacher": t.pk,
        f"teacher_{world['maths'].pk}": t2.pk,
        f"compulsory_{world['maths'].pk}": "on",
    }
    resp = client_a.post(url(arm), data)
    assert resp.status_code == 302
    with schema_context("school_a"):
        assert FormTeacher.objects.get(arm=arm).teacher == t
        off = SubjectOffering.objects.get(subject=world["maths"], arm=arm)
        assert off.teacher == t2 and off.is_compulsory
        assert not SubjectOffering.objects.filter(subject=world["basic"]).exists()
        assert len(audits("academics.offering_assigned")) == 1
        assert len(audits("academics.form_teacher_assigned")) == 1
        after = audits("academics.offering_assigned")[0].after
        assert after == {
            "what": "offering",
            "subject_id": world["maths"].pk,
            "arm_id": arm.pk,
            "session_id": world["current"].pk,
            "teacher_id": t2.pk,
        }
    # Re-posting identical data audits nothing new.
    client_a.post(url(arm), data)
    with schema_context("school_a"):
        assert len(audits("academics.offering_assigned")) == 1
        assert len(audits("academics.form_teacher_assigned")) == 1
    # Clearing the form teacher and a subject teacher.
    client_a.post(url(arm), {"form_teacher": "", f"teacher_{world['maths'].pk}": ""})
    with schema_context("school_a"):
        assert not FormTeacher.objects.filter(arm=arm).exists()
        off = SubjectOffering.objects.get(subject=world["maths"], arm=arm)
        assert off.teacher is None and off.is_compulsory is False


def test_arm_detail_without_current_term(world, client_a):
    with schema_context("school_a"):
        Term.objects.update(is_current=False)
        login_verified(client_a, make_staff("school_admin"))
    resp = client_a.get(url(world["jss_arm"]))
    body = resp.content.decode()
    assert "Set a current term first" in body
    assert "<select" not in body
    assert client_a.post(url(world["jss_arm"]), {}).status_code == 200


def test_arm_detail_only_lists_applicable_subjects(world, client_a):
    with schema_context("school_a"):
        login_verified(client_a, make_staff("school_admin"))
    body = client_a.get(url(world["sss_arm"])).content.decode()
    assert "Maths" in body and "Basic Sci" not in body


def test_structure_links_to_arm_detail(world, client_a):
    with schema_context("school_a"):
        login_verified(client_a, make_staff("school_admin"))
    assert url(world["jss_arm"]) in client_a.get(reverse("academics:structure")).content.decode()
