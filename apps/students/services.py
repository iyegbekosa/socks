"""Admission, profile updates, photos and withdrawal of students.

Date of birth, photos and guardian contacts are children's data: never log them and never
put them in audit before/after values (record field names instead).
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.academics.models import AcademicSession, ClassArm
from apps.academics.services import (
    check_session_open,
    next_admission_number,
    validate_manual_admission_number,
)
from apps.accounts.services import create_student_user
from apps.audit.services import record
from apps.core.images import clean_image
from apps.students.models import Enrolment, Student
from apps.students.passwords import generate_one_time_password
from apps.tenancy.limits import PlanLimits

logger = logging.getLogger(__name__)

MIN_AGE, MAX_AGE = 1, 25
PHOTO_MAX_BYTES = 2 * 1024 * 1024
PHOTO_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
PHOTO_MAX_PIXELS = 4096 * 4096
PHOTO_SIDE = 600
PHOTO_QUALITY = 80

# Fields update_student may change. Status, user and consent have their own services.
EDITABLE_FIELDS = (
    "first_name",
    "middle_name",
    "last_name",
    "sex",
    "date_of_birth",
    "guardian_name",
    "guardian_relationship",
    "guardian_phone",
    "guardian_email",
    "guardian2_name",
    "guardian2_relationship",
    "guardian2_phone",
    "guardian2_email",
)


@dataclass(frozen=True)
class GuardianData:
    name: str
    relationship: str
    phone: str | None
    email: str

    def as_fields(self, prefix: str) -> dict[str, Any]:
        return {
            f"{prefix}_name": self.name or "",
            f"{prefix}_relationship": self.relationship or "",
            f"{prefix}_phone": self.phone or None,
            f"{prefix}_email": self.email or "",
        }


def _age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_date_of_birth(dob: date | None) -> None:
    if dob is None:
        raise ValidationError({"date_of_birth": "Enter the date of birth."})
    today = timezone.localdate()
    if dob >= today:
        raise ValidationError({"date_of_birth": "The date of birth must be in the past."})
    age = _age_on(dob, today)
    if not MIN_AGE <= age <= MAX_AGE:
        raise ValidationError(
            {"date_of_birth": f"A student must be {MIN_AGE} to {MAX_AGE} years old."}
        )


def _check_arm_and_session(arm: ClassArm | None, session: AcademicSession | None):
    errors: dict[str, str] = {}
    arm_pk, session_pk = getattr(arm, "pk", None), getattr(session, "pk", None)
    fresh_arm = ClassArm.objects.filter(pk=arm_pk).first() if arm_pk is not None else None
    fresh_session = (
        AcademicSession.objects.filter(pk=session_pk).first() if session_pk is not None else None
    )
    if fresh_arm is None:
        errors["arm"] = "Choose a class that exists."
    if fresh_session is None:
        errors["session"] = "Choose a session that exists."
    if errors:
        raise ValidationError(errors)
    check_session_open(fresh_session)  # type: ignore[arg-type]
    return fresh_arm, fresh_session


def _merge(errors: dict[str, list[str]], exc: ValidationError) -> None:
    for key, messages in exc.message_dict.items():
        errors.setdefault(key, []).extend(messages)


def admit_student(
    *,
    first_name: str,
    middle_name: str = "",
    last_name: str,
    sex: str,
    date_of_birth: date,
    arm: ClassArm,
    session: AcademicSession,
    guardian: GuardianData,
    guardian2: GuardianData | None = None,
    consent_method: str,
    admission_number: str | None = None,
    by: Any,
    request: Any = None,
    issue_password: bool = True,
) -> tuple[Student, str | None]:
    """Admit one student: login, profile (with guardian contacts) and enrolment.

    Returns ``(student, one_time_password)``; the password is ``None`` when
    ``issue_password=False`` (bulk import issues passwords later, avoiding Argon2 per row).
    Raises ``ValidationError`` (field-keyed) or ``PlanLimitExceeded``; nothing is written then.
    """
    with transaction.atomic():
        PlanLimits.for_current_school().check_can_add_students(1)
        arm, session = _check_arm_and_session(arm, session)
        student = Student(
            first_name=first_name,
            middle_name=middle_name or "",
            last_name=last_name,
            sex=sex,
            date_of_birth=date_of_birth,
            consent_method=consent_method,
            consent_recorded_by=by,
            **guardian.as_fields("guardian"),
            **(guardian2 or GuardianData("", "", None, "")).as_fields("guardian2"),
        )
        errors: dict[str, list[str]] = {}
        try:
            validate_date_of_birth(date_of_birth)
        except ValidationError as exc:
            _merge(errors, exc)
        try:
            student.full_clean(exclude=["user", "date_of_birth"] if errors else ["user"])
        except ValidationError as exc:
            _merge(errors, exc)
        number: str | None = None
        if (admission_number or "").strip():
            try:
                number = validate_manual_admission_number(admission_number or "")
            except ValidationError as exc:
                errors.setdefault("admission_number", []).extend(exc.messages)
        if errors:
            raise ValidationError(errors)
        if number is None:
            number = next_admission_number(session.start_date.year)
        password = generate_one_time_password() if issue_password else None
        student.user = create_student_user(
            admission_number=number,
            first_name=student.first_name,
            last_name=student.last_name,
            password=password,
        )
        student.save()
        enrolment = Enrolment(student=student, arm=arm, session=session)
        enrolment.full_clean()
        enrolment.save()
        record(
            "student.admitted",
            request=request,
            actor=by,
            obj=student,
            after={
                "student_id": student.pk,
                "arm_id": arm.pk,
                "session_id": session.pk,
                "admission_number": student.user.admission_number,
            },
        )
    return student, password


def update_student(student: Student, data: dict[str, Any], *, by: Any, request: Any = None):
    """Update profile and guardian fields. ``data`` maps field names to values; a
    ``GuardianData`` under ``"guardian"`` / ``"guardian2"`` sets that guardian's fields."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("guardian", "guardian2") and isinstance(value, GuardianData):
            flat.update(value.as_fields(key))
        else:
            flat[key] = value
    unknown = sorted(set(flat) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValidationError(f"These fields cannot be changed here: {', '.join(unknown)}.")
    with transaction.atomic():
        current = Student.objects.select_for_update().get(pk=student.pk)
        candidate = Student.objects.get(pk=student.pk)
        for key, value in flat.items():
            setattr(candidate, key, value)
        candidate.full_clean(exclude=["user"])
        changed = [f for f in EDITABLE_FIELDS if getattr(candidate, f) != getattr(current, f)]
        if "date_of_birth" in changed:
            validate_date_of_birth(candidate.date_of_birth)
        if not changed:
            return student
        candidate.save(update_fields=changed)
        user_fields = [f for f in ("first_name", "last_name") if f in changed]
        if user_fields:
            user = candidate.user
            for name in user_fields:
                setattr(user, name, getattr(candidate, name))
            user.save(update_fields=user_fields)
        for f in changed:
            setattr(student, f, getattr(candidate, f))
        record(
            "student.updated",
            request=request,
            actor=by,
            obj=student,
            after={"changed": changed},
        )
    return student


def _delete_old_photo(name: str) -> None:
    """Best-effort cleanup after commit; a storage failure must not undo a saved photo."""
    try:
        default_storage.delete(name)
    except Exception:
        logger.exception("Could not delete replaced student photo %s", name)


def set_student_photo(student: Student, upload, *, by: Any, request: Any = None) -> None:
    """Validate, re-encode (JPEG, ≤600 px, quality 80) and store a private photo."""
    content = clean_image(
        upload,
        max_bytes=PHOTO_MAX_BYTES,
        allowed=PHOTO_FORMATS,
        max_pixels=PHOTO_MAX_PIXELS,
        output="jpeg",
        max_side=PHOTO_SIDE,
        quality=PHOTO_QUALITY,
        label="Photo",
    )
    with transaction.atomic():
        old_name = (
            Student.objects.select_for_update()
            .filter(pk=student.pk)
            .values_list("photo", flat=True)
            .first()
            or ""
        )
        student.photo.save(content.name or "photo.jpg", content, save=False)
        student.save(update_fields=["photo"])
        new_name = student.photo.name or ""
        if old_name and old_name != new_name:
            transaction.on_commit(lambda: _delete_old_photo(old_name))
        record("student.photo_changed", request=request, actor=by, obj=student)


def withdraw_student(student: Student, *, by: Any, request: Any = None) -> None:
    """Withdraw: status, latest active enrolment and the login are all switched off."""
    with transaction.atomic():
        current = Student.objects.select_for_update().get(pk=student.pk)
        if current.status == Student.Status.WITHDRAWN:
            return
        current.status = Student.Status.WITHDRAWN
        current.save(update_fields=["status"])
        enrolment = (
            current.enrolments.filter(status=Enrolment.Status.ACTIVE)
            .order_by("-session__start_date")
            .first()
        )
        if enrolment is not None:
            enrolment.status = Enrolment.Status.WITHDRAWN
            enrolment.save(update_fields=["status"])
        user = current.user
        user.is_active = False
        user.save(update_fields=["is_active"])
        student.status = current.status
        record(
            "student.withdrawn",
            request=request,
            actor=by,
            obj=current,
            after={"enrolment_id": enrolment.pk if enrolment else None},
        )
