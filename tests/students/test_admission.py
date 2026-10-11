import io
from datetime import date, timedelta
from unittest import mock

import cloudinary
import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context
from PIL import Image

from apps.academics.models import AcademicSession, AcademicSettings, AdmissionCounter
from apps.academics.services import update_admission_settings
from apps.accounts.roles import set_roles
from apps.accounts.services import reset_phone_for
from apps.audit.models import AuditEvent
from apps.students.models import Enrolment, Student
from apps.students.passwords import ALPHABET, generate_one_time_password
from apps.students.services import (
    GuardianData,
    admit_student,
    set_student_photo,
    update_student,
    withdraw_student,
)
from apps.tenancy.limits import PlanLimitExceeded, PlanLimits
from apps.tenancy.models import Plan
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.students.factories import StudentProfileFactory, default_arm, default_session

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def _clean(school_a):
    with schema_context("school_a"):
        AcademicSettings.objects.all().delete()
        AdmissionCounter.objects.all().delete()
        update_admission_settings(prefix="GFS", pattern="{PREFIX}/{YEAR}/{SEQ:4}", by=None)
    yield


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture
def upload():
    with mock.patch("cloudinary.uploader.upload", return_value={"public_id": "x"}) as m:
        yield m


@pytest.fixture
def admin(school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["school_admin"], by=u)
    return u


GUARDIAN = GuardianData(
    name="Ngozi Eze", relationship="mother", phone="0803 123 4567", email="Ngozi@Mail.NG"
)


def admit(admin, **over):
    data = dict(
        first_name="Ada",
        middle_name="Chioma",
        last_name="Eze",
        sex="F",
        date_of_birth=date(2014, 5, 17),
        arm=default_arm(),
        session=default_session(),
        guardian=GUARDIAN,
        consent_method="paper_form",
        by=admin,
    )
    data.update(over)
    return admit_student(**data)


def counts():
    return (
        User.objects.filter(kind="student").count(),
        Student.objects.count(),
        Enrolment.objects.count(),
        AuditEvent.objects.filter(action="student.admitted").count(),
    )


def test_one_time_password_shape():
    pw = generate_one_time_password()
    assert len(pw) == 10 and set(pw) <= set(ALPHABET)
    assert "0" not in ALPHABET and "O" not in ALPHABET and "I" not in ALPHABET
    assert "1" not in ALPHABET and "L" in ALPHABET


def test_admission_creates_student_user_and_enrolment(admin):
    with schema_context("school_a"):
        student, password = admit(admin)
        assert student.admission_number == "GFS/2026/0001"
        user = student.user
        assert user.kind == "student" and user.must_change_password is True
        assert (user.first_name, user.last_name) == ("Ada", "Eze")
        assert authenticate(identifier="gfs/2026/0001", password=password) == user
        assert student.guardian_phone == "+2348031234567"
        assert student.guardian_email == "ngozi@mail.ng"
        assert student.consent_recorded_by == admin and student.consent_recorded_at
        enrolment = Enrolment.objects.get(student=student)
        assert (enrolment.arm, enrolment.session, enrolment.status) == (
            default_arm(),
            default_session(),
            "active",
        )
        event = AuditEvent.objects.get(action="student.admitted")
    assert event.after == {
        "student_id": student.pk,
        "arm_id": enrolment.arm_id,
        "session_id": enrolment.session_id,
        "admission_number": "GFS/2026/0001",
    }
    text = str(event.before) + str(event.after)
    for secret in ("2014", "8031234567", "ngozi", password):
        assert secret not in text.lower()


def test_admission_without_password(admin):
    with schema_context("school_a"):
        student, password = admit(admin, issue_password=False)
        assert password is None
        assert not student.user.has_usable_password()
        assert student.user.must_change_password is True


def test_second_admission_gets_next_number(admin):
    with schema_context("school_a"):
        admit(admin)
        student, _ = admit(admin, first_name="Bola")
        assert student.admission_number == "GFS/2026/0002"


def test_staff_default_must_change_password_false(school_a):
    with schema_context("school_a"):
        assert StaffFactory().must_change_password is False


def test_plan_limit_blocks_admission_and_creates_nothing(admin, school_a):
    plan = Plan.objects.create(
        code="tiny", name="Tiny", max_students=2, storage_bytes=10**9, sms_credits_per_term=0
    )
    old = school_a.plan
    school_a.plan = plan
    school_a.save()
    try:
        with schema_context("school_a"):
            StudentProfileFactory.create_batch(2)
            before = counts()
            with pytest.raises(PlanLimitExceeded):
                admit(admin)
            assert counts() == before
            assert not AdmissionCounter.objects.filter(last__gt=0).exists()
    finally:
        school_a.plan = old
        school_a.save()


def test_manual_admission_number(admin):
    with schema_context("school_a"):
        student, _ = admit(admin, admission_number=" old/1999/12 ")
        assert student.admission_number == "OLD/1999/12"
        assert not AdmissionCounter.objects.filter(last__gt=0).exists()


def test_manual_phone_shaped_admission_number_refused(admin):
    with schema_context("school_a"):
        before = counts()
        with pytest.raises(ValidationError) as exc:
            admit(admin, admission_number="0803 123 4567")
        assert "admission_number" in exc.value.message_dict
        assert counts() == before


def test_guardian_needs_phone_or_email(admin):
    with schema_context("school_a"):
        before = counts()
        bare = GuardianData(name="Ngozi Eze", relationship="mother", phone="", email="")
        with pytest.raises(ValidationError) as exc:
            admit(admin, guardian=bare)
        assert "phone number or email" in str(exc.value)
        assert counts() == before
        admit(admin, guardian=GuardianData("Ngozi Eze", "mother", None, "n@mail.ng"))
        admit(admin, guardian=GuardianData("Ngozi Eze", "mother", "08031234567", ""))


def test_guardian_contact_db_constraint(school_a):
    with schema_context("school_a"):
        student = StudentProfileFactory()
        Student.objects.filter(pk=student.pk).update(guardian_email="x@y.ng")
        with pytest.raises(IntegrityError), transaction.atomic():
            Student.objects.filter(pk=student.pk).update(guardian_phone=None, guardian_email="")


def test_invalid_guardian_phone_is_field_error(admin):
    with schema_context("school_a"):
        bad = GuardianData(name="Ngozi Eze", relationship="mother", phone="12345", email="")
        with pytest.raises(ValidationError) as exc:
            admit(admin, guardian=bad)
        assert "guardian_phone" in exc.value.message_dict
        bad2 = GuardianData(name="Obi", relationship="father", phone="999", email="")
        with pytest.raises(ValidationError) as exc:
            admit(admin, guardian2=bad2)
        assert "guardian2_phone" in exc.value.message_dict


def test_second_guardian_saved(admin):
    with schema_context("school_a"):
        g2 = GuardianData(name="Obi Eze", relationship="father", phone=None, email="OBI@x.ng")
        student, _ = admit(admin, guardian2=g2)
        student.refresh_from_db()
        assert (student.guardian2_name, student.guardian2_email) == ("Obi Eze", "obi@x.ng")


def years_before(today, years):
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 February
        return today.replace(year=today.year - years, day=28)


@pytest.mark.parametrize(
    "years,shift,ok",
    [
        (0, -1, False),  # born yesterday: age 0
        (1, 1, False),  # one tomorrow: still 0
        (1, 0, True),  # first birthday today
        (26, 1, True),  # 26 tomorrow: still 25
        (26, 0, False),  # 26 today
    ],
)
def test_age_bounds(admin, years, shift, ok):
    dob = years_before(timezone.localdate(), years) + timedelta(days=shift)
    with schema_context("school_a"):
        if ok:
            admit(admin, date_of_birth=dob)
        else:
            with pytest.raises(ValidationError) as exc:
                admit(admin, date_of_birth=dob)
            assert "date_of_birth" in exc.value.message_dict


def test_future_date_of_birth_refused(admin):
    with schema_context("school_a"):
        with pytest.raises(ValidationError) as exc:
            admit(admin, date_of_birth=timezone.localdate() + timedelta(days=1))
        assert "date_of_birth" in exc.value.message_dict


def test_ended_session_refused(admin):
    with schema_context("school_a"):
        old = AcademicSession.objects.create(
            name="2020/2021", start_date=date(2020, 9, 1), end_date=date(2021, 7, 31)
        )
        before = counts()
        with pytest.raises(ValidationError) as exc:
            admit(admin, session=old)
        assert "session" in exc.value.message_dict
        assert counts() == before


def test_missing_arm_refused(admin):
    with schema_context("school_a"):
        arm = default_arm()
        arm.pk = 999_999
        with pytest.raises(ValidationError) as exc:
            admit(admin, arm=arm)
        assert "arm" in exc.value.message_dict


def test_required_names_and_choices(admin):
    with schema_context("school_a"):
        with pytest.raises(ValidationError) as exc:
            admit(admin, first_name=" ", sex="X", consent_method="sms")
        assert {"first_name", "sex", "consent_method"} <= set(exc.value.message_dict)


def png_bytes(size=(1200, 800), fmt="PNG", mode="RGB", color="red"):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def test_photo_upload_stored_as_small_private_jpeg(admin, upload):
    with schema_context("school_a"):
        student = StudentProfileFactory()
        set_student_photo(student, SimpleUploadedFile("me.png", png_bytes()), by=admin)
        student.refresh_from_db()
        assert student.photo.name.startswith("students/photos/")
        assert student.photo.name.endswith(".jpg")
        assert AuditEvent.objects.filter(action="student.photo_changed").count() == 1
    kwargs = upload.call_args.kwargs
    assert kwargs["type"] == "authenticated"
    assert kwargs["public_id"].startswith("dev/school_a/students/photos/")
    img = Image.open(io.BytesIO(upload.call_args.args[0]))
    assert img.format == "JPEG" and max(img.size) == 600


def test_photo_gif_refused(admin, upload):
    with schema_context("school_a"):
        student = StudentProfileFactory()
        with pytest.raises(ValidationError):
            set_student_photo(student, SimpleUploadedFile("a.gif", png_bytes(fmt="GIF")), by=admin)
        student.refresh_from_db()
        assert not student.photo
    upload.assert_not_called()


def test_replaced_photo_deleted_on_commit(admin, upload, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        student = StudentProfileFactory()
        set_student_photo(student, SimpleUploadedFile("a.png", png_bytes()), by=admin)
        old = student.photo.name
        with mock.patch("cloudinary.uploader.destroy") as destroy:
            with django_capture_on_commit_callbacks(execute=False) as callbacks:
                set_student_photo(student, SimpleUploadedFile("b.png", png_bytes()), by=admin)
            destroy.assert_not_called()
            for cb in callbacks:
                cb()
        assert destroy.call_args.args[0] == "dev/school_a/" + old.removesuffix(".jpg")


def test_photo_delete_failure_logged_not_raised(
    admin, upload, django_capture_on_commit_callbacks, caplog
):
    with schema_context("school_a"):
        student = StudentProfileFactory()
        set_student_photo(student, SimpleUploadedFile("a.png", png_bytes()), by=admin)
        old = student.photo.name
        with mock.patch("cloudinary.uploader.destroy", side_effect=RuntimeError("boom")):
            with django_capture_on_commit_callbacks(execute=True):
                set_student_photo(student, SimpleUploadedFile("b.png", png_bytes()), by=admin)
        assert student.photo.name != old
    assert any(r.levelname == "ERROR" and old in r.getMessage() for r in caplog.records)


def test_withdraw_deactivates_login(admin):
    with schema_context("school_a"):
        student, password = admit(admin)
        withdraw_student(student, by=admin)
        student.refresh_from_db()
        assert student.status == "withdrawn"
        assert not student.user.is_active
        assert Enrolment.objects.get(student=student).status == "withdrawn"
        assert authenticate(identifier=student.admission_number, password=password) is None
        assert AuditEvent.objects.filter(
            action="student.withdrawn", object_id=str(student.pk)
        ).exists()


def test_update_student_audits_field_names_only(admin):
    with schema_context("school_a"):
        student, _ = admit(admin)
        update_student(
            student,
            {"first_name": "Adaeze", "guardian_email": "NEW@Mail.ng", "sex": "F"},
            by=admin,
        )
        student.refresh_from_db()
        assert student.first_name == "Adaeze" and student.user.first_name == "Adaeze"
        assert student.guardian_email == "new@mail.ng"
        event = AuditEvent.objects.get(action="student.updated")
    assert event.after == {"changed": ["first_name", "guardian_email"]}


def test_update_student_rejects_removing_all_guardian_contacts(admin):
    with schema_context("school_a"):
        student, _ = admit(admin)
        with pytest.raises(ValidationError):
            update_student(student, {"guardian_phone": "", "guardian_email": ""}, by=admin)
        student.refresh_from_db()
        assert student.guardian_phone == "+2348031234567"


def test_update_student_rejects_unknown_fields(admin):
    with schema_context("school_a"):
        student, _ = admit(admin)
        with pytest.raises(ValidationError):
            update_student(student, {"status": "graduated"}, by=admin)


def test_student_count_counts_active_student_rows(school_a):
    with schema_context("school_a"):
        StudentFactory()  # login without a profile: not counted
        StudentProfileFactory.create_batch(2)
        StudentProfileFactory(status="withdrawn")
        assert PlanLimits.for_current_school().student_count() == 2


def test_reset_phone_for(school_a):
    with schema_context("school_a"):
        student = StudentProfileFactory(guardian_phone="0803 765 4321")
        assert reset_phone_for(student.user) == "+2348037654321"
        assert reset_phone_for(StudentFactory()) is None
        email_only = StudentProfileFactory(guardian_phone=None, guardian_email="a@b.ng")
        assert reset_phone_for(email_only.user) is None
        staff = StaffFactory()
        assert reset_phone_for(staff) == staff.phone
