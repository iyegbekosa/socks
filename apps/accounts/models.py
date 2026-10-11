from uuid import uuid4

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.accounts.identifiers import normalise_admission_number, normalise_ng_phone

MFA_REQUIRED_ROLE_NAMES = frozenset({"school_admin", "principal", "bursar"})


class User(AbstractUser):
    class Kind(models.TextChoices):
        STAFF = "staff", "Staff"
        STUDENT = "student", "Student"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.STAFF)
    phone = models.CharField(max_length=16, unique=True, null=True, blank=True)  # noqa: DJ001
    admission_number = models.CharField(max_length=40, unique=True, null=True, blank=True)  # noqa: DJ001
    recovery_phone = models.CharField(max_length=16, null=True, blank=True)  # noqa: DJ001

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["email"], condition=~Q(email=""), name="accounts_user_email_unique"
            )
        ]

    def _normalise(self) -> None:
        if not self.username:
            self.username = f"u_{uuid4().hex[:12]}"
        self.email = (self.email or "").strip().lower()
        self.phone = normalise_ng_phone(self.phone) if self.phone else None
        self.recovery_phone = (
            normalise_ng_phone(self.recovery_phone) if self.recovery_phone else None
        )
        self.admission_number = normalise_admission_number(self.admission_number or "") or None

    def clean_fields(self, exclude=None):  # type: ignore[no-untyped-def]
        if not self.username:
            self.username = f"u_{uuid4().hex[:12]}"
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        for field in ("phone", "recovery_phone"):
            value = getattr(self, field)
            if value:
                try:
                    normalise_ng_phone(value)
                except ValueError as exc:
                    errors[field] = str(exc)
        if errors:
            raise ValidationError(errors)
        self._normalise()

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._normalise()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "username",
                "email",
                "phone",
                "recovery_phone",
                "admission_number",
            }
        super().save(*args, **kwargs)

    @property
    def roles(self) -> frozenset[str]:
        if not self.pk:
            return frozenset()
        return frozenset(self.groups.values_list("name", flat=True))

    @property
    def is_student(self) -> bool:
        return self.kind == self.Kind.STUDENT

    @property
    def requires_mfa(self) -> bool:
        return bool(self.roles & MFA_REQUIRED_ROLE_NAMES)

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))
