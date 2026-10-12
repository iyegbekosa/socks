from typing import Any

from django.db.models import QuerySet

from apps.academics.models import SubjectOffering
from apps.academics.services import current_session
from apps.accounts.roles import TEACHER
from apps.results.models import Score
from apps.students.models import Enrolment, Student


def students_taking(offering: SubjectOffering) -> QuerySet[Student]:
    """Active students taking the offering, ordered by last then first name."""
    qs = Student.objects.filter(
        status=Student.Status.ACTIVE,
        enrolments__arm_id=offering.arm_id,
        enrolments__session_id=offering.session_id,
        enrolments__status=Enrolment.Status.ACTIVE,
    )
    if not offering.is_compulsory:
        qs = qs.filter(elective_choices__offering=offering)
    return qs.distinct().order_by("last_name", "first_name", "pk")


def can_enter_scores(user: Any, offering: SubjectOffering) -> bool:
    """Only the active teacher assigned to the offering enters its scores (FR-06)."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_student or offering.teacher_id is None or offering.teacher_id != user.pk:
        return False
    return bool(user.has_role(TEACHER))


def has_scores(section: str) -> bool:
    """True once any entered (non-null) score exists for the section in the current session."""
    session = current_session()
    if session is None:
        return False
    return Score.objects.filter(
        value__isnull=False,
        offering__arm__level__section=section,
        term__session=session,
    ).exists()


def has_student_scores(offering: SubjectOffering, student: Student) -> bool:
    """True once the student has any entered (non-null) score for the offering."""
    return Score.objects.filter(offering=offering, student=student, value__isnull=False).exists()
