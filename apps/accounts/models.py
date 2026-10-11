from collections.abc import Iterable
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
            ),
            models.CheckConstraint(
                condition=~Q(kind="staff")
                | (Q(admission_number__isnull=True) & (Q(phone__isnull=False) | ~Q(email=""))),
                name="accounts_user_staff_identifiers",
            ),
            models.CheckConstraint(
                condition=~Q(kind="student")
                | (Q(admission_number__isnull=False) & Q(phone__isnull=True) & Q(email="")),
                name="accounts_user_student_identifiers",
            ),
        ]

    _NORMALISERS = {
        "email": lambda v: (v or "").strip().lower(),
        "phone": lambda v: normalise_ng_phone(v) if v else None,
        "recovery_phone": lambda v: normalise_ng_phone(v) if v else None,
        "admission_number": lambda v: normalise_admission_number(v or "") or None,
    }

    def _normalise(self, only: Iterable[str] | None = None) -> None:
        """Normalise identifier fields in place. Invalid phones raise ValidationError."""
        if not self.username and (only is None or "username" in only):
            self.username = f"u_{uuid4().hex[:12]}"
        errors: dict[str, str] = {}
        for name, fn in self._NORMALISERS.items():
            if only is not None and name not in only:
                continue
            try:
                setattr(self, name, fn(getattr(self, name)))
            except ValueError as exc:
                errors[name] = str(exc)
        if errors:
            raise ValidationError(errors)

    def clean_fields(self, exclude=None):  # type: ignore[no-untyped-def]
        # Normalise first so max_length and uniqueness see the canonical value.
        errors: dict[str, list[str]] = {}
        skip = set(exclude or ())
        for name in ("username", *self._NORMALISERS):
            if name in skip:
                continue
            try:
                self._normalise([name])
            except ValidationError as exc:
                errors.update(exc.message_dict)
                skip.add(name)
        try:
            super().clean_fields(exclude=skip)
        except ValidationError as exc:
            errors.update(exc.message_dict)
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        update_fields = kwargs.get("update_fields")
        self._normalise(None if update_fields is None else set(update_fields))
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
