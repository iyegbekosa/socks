from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from apps.accounts.identifiers import classify_identifier


class IdentifierBackend(ModelBackend):
    """Log in by email/phone (staff only) or admission number (students only)."""

    def authenticate(  # type: ignore[override]
        self,
        request: Any,
        identifier: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if not identifier or password is None:
            return None
        User = get_user_model()
        kind, value = classify_identifier(identifier)
        qs = User.objects.all()
        if kind == "email":
            qs = qs.filter(kind="staff", email=value)
        elif kind == "phone":
            qs = qs.filter(kind="staff", phone=value)
        else:
            qs = qs.filter(kind="student", admission_number=value)
        user = qs.first()
        if user is None:
            User().set_password(password)  # equalise timing
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
