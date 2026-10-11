from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.academics.models import AcademicSession, ClassArm
from apps.accounts.identifiers import normalise_ng_phone
from apps.core.storage import unique_upload_name

RELATIONSHIP_CHOICES = [
    ("mother", "Mother"),
    ("father", "Father"),
    ("guardian", "Guardian"),
    ("other", "Other"),
]
PHONE_FIELDS = ("guardian_phone", "guardian2_phone")
EMAIL_FIELDS = ("guardian_email", "guardian2_email")
NAME_FIELDS = ("first_name", "middle_name", "last_name", "guardian_name", "guardian2_name")
GUARDIAN_CONTACT_MESSAGE = "Give the main guardian's phone number or email address."


def photo_upload_to(instance: "Student", filename: str) -> str:
    return unique_upload_name("students/photos", filename)


class Student(models.Model):
    class Sex(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        WITHDRAWN = "withdrawn", "Withdrawn"
        GRADUATED = "graduated", "Graduated"

    class ConsentMethod(models.TextChoices):
        PAPER_FORM = "paper_form", "Paper form"
        IN_PERSON = "in_person", "In person"
        DIGITAL = "digital", "Digital"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="student",
        limit_choices_to={"kind": "student"},
    )
    first_name = models.CharField(max_length=80)
    middle_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80)
    sex = models.CharField(max_length=1, choices=Sex.choices)
    date_of_birth = models.DateField()
    photo = models.FileField(upload_to=photo_upload_to, blank=True, max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    admitted_at = models.DateTimeField(default=timezone.now)

    guardian_name = models.CharField(max_length=120)
    guardian_relationship = models.CharField(max_length=10, choices=RELATIONSHIP_CHOICES)
    guardian_phone = models.CharField(max_length=16, null=True, blank=True)  # noqa: DJ001 - E.164 or None
    guardian_email = models.EmailField(blank=True)

    guardian2_name = models.CharField(max_length=120, blank=True)
    guardian2_relationship = models.CharField(
        max_length=10, choices=RELATIONSHIP_CHOICES, blank=True
    )
    guardian2_phone = models.CharField(max_length=16, null=True, blank=True)  # noqa: DJ001 - E.164 or None
    guardian2_email = models.EmailField(blank=True)

    consent_recorded_at = models.DateTimeField(default=timezone.now)
    consent_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    consent_method = models.CharField(max_length=12, choices=ConsentMethod.choices)

    class Meta:
        ordering = ["last_name", "first_name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(guardian_phone__isnull=False) | ~Q(guardian_email=""),
                name="students_student_guardian_contact",
                violation_error_message=GUARDIAN_CONTACT_MESSAGE,
            ),
        ]

    def __str__(self) -> str:
        return self.full_name

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        update_fields = kwargs.get("update_fields")
        self._normalise(None if update_fields is None else set(update_fields))
        super().save(*args, **kwargs)

    @property
    def admission_number(self) -> str | None:
        return self.user.admission_number

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)

    def _normalise(self, only: set[str] | None = None) -> None:
        """Canonical contacts: E.164 phones (blank → None), lower-case emails, trimmed names."""
        errors: dict[str, str] = {}
        for name in NAME_FIELDS:
            if only is None or name in only:
                setattr(self, name, " ".join((getattr(self, name) or "").split()))
        for name in EMAIL_FIELDS:
            if only is None or name in only:
                setattr(self, name, (getattr(self, name) or "").strip().lower())
        for name in PHONE_FIELDS:
            if only is not None and name not in only:
                continue
            raw = (getattr(self, name) or "").strip()
            try:
                setattr(self, name, normalise_ng_phone(raw) if raw else None)
            except ValueError as exc:
                errors[name] = str(exc)
        if errors:
            raise ValidationError(errors)

    def clean_fields(self, exclude=None):  # type: ignore[no-untyped-def]
        errors: dict[str, list[str]] = {}
        skip = set(exclude or ())
        try:
            self._normalise()
        except ValidationError as exc:
            errors.update(exc.message_dict)
            skip.update(exc.message_dict)
        try:
            super().clean_fields(exclude=skip)
        except ValidationError as exc:
            errors.update(exc.message_dict)
        if errors:
            raise ValidationError(errors)

    def clean(self) -> None:
        has_second = self.guardian2_relationship or self.guardian2_phone or self.guardian2_email
        if has_second and not self.guardian2_name:
            raise ValidationError({"guardian2_name": "Give the second guardian's name."})


class Enrolment(models.Model):
    """A student's class for one session. One row per session; history is kept."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        WITHDRAWN = "withdrawn", "Withdrawn"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="enrolments")
    arm = models.ForeignKey(ClassArm, on_delete=models.PROTECT, related_name="enrolments")
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="enrolments"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "session"], name="students_enrolment_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student} / {self.arm} / {self.session}"
