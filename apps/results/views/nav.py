from django.urls import reverse

from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN


def class_back_url(user, arm) -> str:
    """Where a class's results pages send "Back": the class page for the roles that may open
    it (academics:arm_detail), otherwise the teacher's own sheets."""
    if user.has_role(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL):
        return reverse("academics:arm_detail", args=[arm.pk])
    return reverse("results:my_sheets")
