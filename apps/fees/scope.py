from typing import Any

from apps.accounts.roles import BURSAR, PROPRIETOR, SCHOOL_ADMIN

MANAGE_ROLES = (BURSAR, SCHOOL_ADMIN)
VIEW_ROLES = (BURSAR, SCHOOL_ADMIN, PROPRIETOR)


def _is_staff(user: Any) -> bool:
    return (
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "kind", None) == "staff"
    )


def can_manage_fees(user: Any) -> bool:
    """Bursar or school admin: may change fee setup, invoices and payments."""
    return _is_staff(user) and user.has_role(*MANAGE_ROLES)


def can_view_fees(user: Any) -> bool:
    """Fee managers plus the proprietor (read-only)."""
    return _is_staff(user) and user.has_role(*VIEW_ROLES)
