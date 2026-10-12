from datetime import date, timedelta
from unittest import mock

import cloudinary
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import (
    AcademicSession,
    AcademicSettings,
    AdmissionCounter,
    ClassArm,
    ClassLevel,
    FormTeacher,
    Term,
)
from apps.academics.services import update_admission_settings
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.students.models import Enrolment, Student
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified
from tests.students.factories import StudentProfileFactory
from tests.students.test_admission import png_bytes

pytestmark = pytest.mark.django_db


def _wipe():
    Enrolment.objects.all().delete()
    Student.objects.all().delete()
    FormTeacher.objects.all().delete()
    Term.objects.all().delete()
    AcademicSession.objects.all().delete()
    ClassArm.objects.all().delete()
    ClassLevel.objects.all().delete()
    AcademicSettings.objects.all().delete()
    AdmissionCounter.objects.all().delete()


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture
def world(school_a):
    with schema_context("school_a"):
        _wipe()
        update_admission_settings(prefix="GFS", pattern="{PREFIX}/{YEAR}/{SEQ:4}", by=None)
        today = date.today()
        session = AcademicSession.objects.create(
            name="2026/2027",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=300),
        )
        term = Term.objects.create(
            session=session,
            number=1,
            start_date=session.start_date,
            end_date=today + timedelta(days=60),
            is_current=True,
        )
        lvl1 = ClassLevel.objects.create(name="JSS 1", section="jss", order=1)
        lvl2 = ClassLevel.objects.create(name="JSS 2", section="jss", order=2)
        a = ClassArm.objects.create(level=lvl1, suffix="A")
        b = ClassArm.objects.create(level=lvl2, suffix="B")
        mine = StudentProfileFactory(
            first_name="Ada", last_name="Okoro", enrolment__arm=a, enrolment__session=session
        )
        other = StudentProfileFactory(
            first_name="Bola", last_name="Yusuf", enrolment__arm=b, enrolment__session=session
        )
        yield {"session": session, "term": term, "a": a, "b": b, "mine": mine, "other": other}
        _wipe()


def staff(school, roles, *, mfa=False):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, roles, by=u)
    return u


def login(client, user):
    if user.has_role("school_admin", "principal", "bursar"):
        login_verified(client, user)
    else:
        with schema_context("school_a"):
            client.force_login(user)


def as_role(client, *roles):
    u = staff(None, list(roles))
    login(client, u)
    return u


def admit_payload(world, **over):
    data = {
        "first_name": "Chika",
        "last_name": "Nwosu",
        "sex": "F",
        "date_of_birth": "12/05/2014",
        "arm": world["a"].pk,
        "guardian_name": "Ngozi Nwosu",
        "guardian_relationship": "mother",
        "guardian_phone": "08031234567",
        "guardian_email": "ngozi@mail.ng",
        "consent": "on",
        "consent_method": "paper_form",
    }
    data.update(over)
    return data


def test_list_search_by_name_and_admission_number(client_a, world):
    as_role(client_a, "school_admin")
    r = client_a.get(reverse("students:list"), {"q": "okoro"})
    assert b"Ada Okoro" in r.content and b"Bola" not in r.content
    number = world["other"].user.admission_number
    r = client_a.get(reverse("students:list"), {"q": number})
    assert b"Bola Yusuf" in r.content and b"Ada" not in r.content
    r = client_a.get(reverse("students:list"), {"arm": world["a"].pk})
    assert b"Ada Okoro" in r.content and b"Bola" not in r.content
    assert b"table-wrap" in r.content


def test_admin_admits_and_sees_password_once(client_a, world):
    as_role(client_a, "school_admin")
    r = client_a.post(reverse("students:admit"), admit_payload(world))
    assert r.status_code == 302
    with schema_context("school_a"):
        s = Student.objects.get(last_name="Nwosu")
        assert AuditEvent.objects.filter(action="student.admitted").exists()
        number = s.user.admission_number
    r = client_a.get(r["Location"])
    assert r.status_code == 200
    assert number.encode() in r.content
    assert r["Cache-Control"] == "no-store"
    assert r["Referrer-Policy"] == "no-referrer"
    assert b"One-time password" in r.content
    again = client_a.get(reverse("students:admitted", args=[s.pk]))
    assert again.status_code == 302
    assert again["Location"] == reverse("students:detail", args=[s.pk])
    assert b"Chika" in client_a.get(reverse("students:list")).content
    with schema_context("school_a"):
        ev = AuditEvent.objects.get(action="student.admitted")
        assert "password" not in str(ev.after).lower()


def test_consent_required(client_a, world):
    as_role(client_a, "school_admin")
    data = admit_payload(world)
    del data["consent"]
    r = client_a.post(reverse("students:admit"), data)
    assert r.status_code == 200
    assert b"Record the guardian" in r.content
    with schema_context("school_a"):
        assert not Student.objects.filter(last_name="Nwosu").exists()


def test_duplicate_manual_number_is_field_error(client_a, world):
    as_role(client_a, "school_admin")
    taken = world["mine"].user.admission_number
    r = client_a.post(reverse("students:admit"), admit_payload(world, admission_number=taken))
    assert r.status_code == 200
    assert r.context["form"].errors.get("admission_number")


def test_missing_guardian_contact_and_bad_phone(client_a, world):
    as_role(client_a, "school_admin")
    r = client_a.post(
        reverse("students:admit"), admit_payload(world, guardian_phone="", guardian_email="")
    )
    assert r.status_code == 200 and r.context["form"].non_field_errors()
    r = client_a.post(reverse("students:admit"), admit_payload(world, guardian_phone="123"))
    assert r.context["form"].errors.get("guardian_phone")


def test_plan_limit_is_non_field_error(client_a, world):
    from apps.tenancy.limits import PlanLimitExceeded

    as_role(client_a, "school_admin")
    with mock.patch(
        "apps.students.views.admit_student", side_effect=PlanLimitExceeded("Plan full.")
    ):
        r = client_a.post(reverse("students:admit"), admit_payload(world))
    assert r.status_code == 200
    assert r.context["form"].non_field_errors() == ["Plan full."]


def test_no_current_session_prompts(client_a, world):
    as_role(client_a, "school_admin")
    with schema_context("school_a"):
        Term.objects.update(is_current=False)
    r = client_a.get(reverse("students:admit"))
    assert b"Set a current term first" in r.content
    assert reverse("academics:sessions").encode() in r.content


def test_teacher_scope_404_for_other_class(client_a, world):
    t = staff(None, ["teacher"])
    with schema_context("school_a"):
        FormTeacher.objects.create(arm=world["a"], session=world["session"], teacher=t)
        Student.objects.filter(pk=world["other"].pk).update(photo="students/photos/x.jpg")
        Student.objects.filter(pk=world["mine"].pk).update(photo="students/photos/m.jpg")
    login(client_a, t)
    assert b"Ada Okoro" in client_a.get(reverse("students:list")).content
    assert b"Bola" not in client_a.get(reverse("students:list")).content
    assert client_a.get(reverse("students:detail", args=[world["mine"].pk])).status_code == 200
    for name in ("detail", "photo", "edit"):
        assert (
            client_a.get(reverse(f"students:{name}", args=[world["other"].pk])).status_code == 404
        )
    # in scope but not an admin: edit is forbidden, withdraw too
    assert client_a.get(reverse("students:edit", args=[world["mine"].pk])).status_code == 403
    assert client_a.post(reverse("students:withdraw", args=[world["mine"].pk])).status_code == 403


def test_photo_redirects_to_signed_url(client_a, world):
    as_role(client_a, "school_admin")
    with schema_context("school_a"):
        Student.objects.filter(pk=world["mine"].pk).update(photo="students/photos/m.jpg")
    r = client_a.get(reverse("students:photo", args=[world["mine"].pk]))
    assert r.status_code == 302
    assert r["Cache-Control"] == "no-store"
    assert "cloudinary.com" in r["Location"] and "expires_at" in r["Location"]
    assert client_a.post(reverse("students:photo", args=[world["mine"].pk])).status_code == 405
    # no photo -> 404
    assert client_a.get(reverse("students:photo", args=[world["other"].pk])).status_code == 404


def test_bursar_sees_basic_fields_only(client_a, world):
    with schema_context("school_a"):
        Student.objects.filter(pk=world["mine"].pk).update(photo="students/photos/m.jpg")
    as_role(client_a, "bursar")
    r = client_a.get(reverse("students:detail", args=[world["mine"].pk]))
    assert r.status_code == 200
    body = r.content.decode()
    assert "Ada Okoro" in body and world["mine"].guardian_name in body
    assert "01/03/2014" not in body and "Date of birth" not in body
    assert "Consent" not in body and "<img" not in body
    assert client_a.get(reverse("students:photo", args=[world["mine"].pk])).status_code == 404


def test_principal_sees_dob(client_a, world):
    as_role(client_a, "principal")
    r = client_a.get(reverse("students:detail", args=[world["mine"].pk]))
    assert "01/03/2014" in r.content.decode()


def test_student_and_anonymous_blocked(client_a, world):
    with schema_context("school_a"):
        s = StudentFactory()
    client_a.force_login(s)
    pk = world["mine"].pk
    for name, args in [
        ("list", []),
        ("admit", []),
        ("detail", [pk]),
        ("admitted", [pk]),
        ("edit", [pk]),
        ("photo", [pk]),
    ]:
        assert client_a.get(reverse(f"students:{name}", args=args)).status_code == 403
    assert client_a.post(reverse("students:withdraw", args=[pk])).status_code == 403
    client_a.logout()
    assert client_a.get(reverse("students:list")).status_code == 302


def test_withdraw_get_405_and_post_audited(client_a, world):
    as_role(client_a, "school_admin")
    url = reverse("students:withdraw", args=[world["mine"].pk])
    assert client_a.get(url).status_code == 405
    r = client_a.post(url)
    assert r.status_code == 302
    with schema_context("school_a"):
        assert Student.objects.get(pk=world["mine"].pk).status == "withdrawn"
        assert AuditEvent.objects.filter(action="student.withdrawn").count() == 1


def test_withdrawn_hidden_from_teacher_visible_to_principal(client_a, world):
    t = staff(None, ["teacher"])
    with schema_context("school_a"):
        FormTeacher.objects.create(arm=world["a"], session=world["session"], teacher=t)
        from apps.students.services import withdraw_student

        withdraw_student(world["mine"], by=None)
    login(client_a, t)
    assert client_a.get(reverse("students:detail", args=[world["mine"].pk])).status_code == 404
    client_a.logout()
    as_role(client_a, "principal")
    assert client_a.get(reverse("students:detail", args=[world["mine"].pk])).status_code == 200


def test_edit_updates_and_audits(client_a, world):
    as_role(client_a, "school_admin")
    pk = world["mine"].pk
    r = client_a.get(reverse("students:edit", args=[pk]))
    assert r.status_code == 200 and b"01/03/2014" in r.content
    data = admit_payload(world, first_name="Adaeze", date_of_birth="01/03/2014")
    for k in ("arm", "consent", "consent_method"):
        data.pop(k)
    r = client_a.post(reverse("students:edit", args=[pk]), data)
    assert r.status_code == 302
    with schema_context("school_a"):
        assert Student.objects.get(pk=pk).first_name == "Adaeze"
        assert AuditEvent.objects.filter(action="student.updated").exists()
    data["guardian_phone"] = "bad"
    r = client_a.post(reverse("students:edit", args=[pk]), data)
    assert r.status_code == 200 and r.context["form"].errors.get("guardian_phone")


def test_edit_photo_upload_audited(client_a, world):
    as_role(client_a, "school_admin")
    pk = world["mine"].pk
    data = admit_payload(world, date_of_birth="01/03/2014")
    for k in ("arm", "consent", "consent_method"):
        data.pop(k)
    data["photo"] = SimpleUploadedFile("me.png", png_bytes())
    with mock.patch("cloudinary.uploader.upload", return_value={"public_id": "x"}):
        r = client_a.post(reverse("students:edit", args=[pk]), data)
    assert r.status_code == 302
    with schema_context("school_a"):
        assert AuditEvent.objects.filter(action="student.photo_changed").exists()


def test_home_links_students_for_staff(client_a, world):
    as_role(client_a, "teacher")
    assert reverse("students:list").encode() in client_a.get(reverse("home")).content
