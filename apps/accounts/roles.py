from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission

from apps.accounts.models import MFA_REQUIRED_ROLE_NAMES

PROPRIETOR = "proprietor"
SCHOOL_ADMIN = "school_admin"
PRINCIPAL = "principal"
BURSAR = "bursar"
TEACHER = "teacher"

STAFF_ROLES = (PROPRIETOR, SCHOOL_ADMIN, PRINCIPAL, BURSAR, TEACHER)
MFA_REQUIRED_ROLES = frozenset({SCHOOL_ADMIN, PRINCIPAL, BURSAR})
assert MFA_REQUIRED_ROLES == MFA_REQUIRED_ROLE_NAMES


def seed_roles(school: Any = None) -> None:
    """Create one Group per role in the current schema (idempotent)."""
    for name in STAFF_ROLES:
        Group.objects.get_or_create(name=name)


def set_roles(user: Any, roles: Iterable[str], *, by: Any = None, request: Any = None) -> None:
    wanted = set(roles)
    unknown = wanted - set(STAFF_ROLES)
    if unknown:
        raise ValueError(f"Unknown roles: {sorted(unknown)}")
    if user.is_student:
        raise ValidationError("Students cannot hold staff roles.")
    current = set(user.roles)
    for name in sorted(wanted - current):
        user.groups.add(Group.objects.get_or_create(name=name)[0])
        # audit: Task 4 wires role.added/role.removed here
    for name in sorted(current - wanted):
        user.groups.remove(Group.objects.get(name=name))
        # audit: Task 4 wires role.added/role.removed here


def require_roles(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path(), str(settings.LOGIN_URL))
            if not user.has_role(*roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


class HasRole(BasePermission):  # type: ignore[misc]
    required_roles: tuple[str, ...] = ()

    @classmethod
    def with_roles(cls, *roles: str) -> type["HasRole"]:
        return type("HasRole_" + "_".join(roles), (cls,), {"required_roles": roles})

    def has_permission(self, request: Any, view: Any) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.has_role(*self.required_roles))
