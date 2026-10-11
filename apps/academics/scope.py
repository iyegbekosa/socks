from typing import Any

from django.db.models import Q, QuerySet

from apps.academics.models import AcademicSession, ClassArm
from apps.accounts.roles import TEACHER


def arms_taught_by(user: Any, session: AcademicSession | None) -> "QuerySet[ClassArm]":
    """Arms where the user is form teacher or teaches an offering in the session."""
    if (
        session is None
        or user is None
        or not getattr(user, "is_active", False)
        or getattr(user, "kind", None) != "staff"
        or not user.has_role(TEACHER)
    ):
        return ClassArm.objects.none()
    return ClassArm.objects.filter(
        Q(form_teachers__teacher=user, form_teachers__session=session)
        | Q(offerings__teacher=user, offerings__session=session)
    ).distinct()
