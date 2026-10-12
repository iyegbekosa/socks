from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet

from apps.academics.models import ClassArm, FormTeacher, SubjectOffering, Term
from apps.academics.services import current_session
from apps.accounts.roles import PRINCIPAL, TEACHER
from apps.results.models import ElectiveChoice, Score
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


def ranked_students(arm: ClassArm, term: Term) -> QuerySet[Student]:
    """Active students with an active enrolment in the arm for the term's session."""
    return Student.objects.filter(
        status=Student.Status.ACTIVE,
        enrolments__arm_id=arm.pk,
        enrolments__session_id=term.session_id,
        enrolments__status=Enrolment.Status.ACTIVE,
    ).distinct()


def required_offerings(arm: ClassArm, term: Term) -> QuerySet[SubjectOffering]:
    """The arm's offerings for the term's session with at least one ranked taker.

    A ranked taker is a student in ``students_taking``: active, actively enrolled in the arm
    and, for an elective, holding an ``ElectiveChoice`` for it. Offerings with no takers need
    no score sheet and produce no results.
    """
    ranked = ranked_students(arm, term)
    takers = Q(
        Exists(ElectiveChoice.objects.filter(offering=OuterRef("pk"), student__in=ranked)),
        is_compulsory=False,
    )
    if ranked.exists():
        takers |= Q(is_compulsory=True)
    return SubjectOffering.objects.filter(arm=arm, session_id=term.session_id).filter(takers)


def can_enter_scores(user: Any, offering: SubjectOffering) -> bool:
    """Only the active teacher assigned to the offering enters its scores (FR-06)."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_student or offering.teacher_id is None or offering.teacher_id != user.pk:
        return False
    return bool(user.has_role(TEACHER))


def can_open_comments(user: Any, arm: ClassArm, term: Term | None) -> bool:
    """Who may open an arm's "Comments & ratings" page: the principal, or a teacher who is the
    arm's form teacher (for the term's session; any session when there is no current term).
    The page and every link to it use this one check."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if getattr(user, "kind", None) != "staff":
        return False
    if user.has_role(PRINCIPAL):
        return True
    if not user.has_role(TEACHER):
        return False
    qs = FormTeacher.objects.filter(arm=arm, teacher=user)
    if term is not None:
        qs = qs.filter(session_id=term.session_id)
    return qs.exists()


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
