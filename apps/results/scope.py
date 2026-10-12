from django.db.models import QuerySet

from apps.academics.models import SubjectOffering
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


def has_scores(section: str) -> bool:
    """True once any score exists for the section in the current session."""
    return False  # Task 4 replaces this


def has_student_scores(offering: SubjectOffering, student: Student) -> bool:
    """True once the student has any score for the offering."""
    return False  # Task 4 replaces this
