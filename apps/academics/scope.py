from typing import TYPE_CHECKING, Any

from django.db.models import Q, QuerySet

from apps.academics.models import AcademicSession, ClassArm
from apps.accounts.roles import BURSAR, PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, TEACHER

if TYPE_CHECKING:
    from apps.students.models import Student


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


def teacher_students(user: Any) -> "QuerySet[Student]":
    """Students with an active enrolment this session in an arm the teacher teaches."""
    from apps.academics.services import current_session
    from apps.students.models import Enrolment, Student

    session = current_session()
    arms = arms_taught_by(user, session)
    return Student.objects.filter(
        enrolments__status=Enrolment.Status.ACTIVE,
        enrolments__session=session,
        enrolments__arm__in=arms,
    ).distinct()


def students_visible_to(user: Any) -> "QuerySet[Student]":
    """Students this user may see; anything outside it is a 404 for them."""
    from apps.students.models import Student

    if (
        user is None
        or not getattr(user, "is_active", False)
        or getattr(user, "kind", None) != "staff"
    ):
        return Student.objects.none()
    if user.has_role(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL, BURSAR):
        return Student.objects.all()
    if user.has_role(TEACHER):
        return teacher_students(user)
    return Student.objects.none()
