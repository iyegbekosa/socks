from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse

from apps.accounts.roles import BURSAR, PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, TEACHER


@login_required
def home(request):
    if request.user.kind == "student":
        return _student_home(request)
    can_see_students = request.user.kind == "staff" and request.user.has_role(
        SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL, BURSAR, TEACHER
    )
    return render(
        request,
        "accounts/home.html",
        {"can_see_students": can_see_students, "register_arms": _form_teacher_arms(request.user)},
    )


def _form_teacher_arms(user):
    """Arms the user form-teaches in the current session (for "Take today's register")."""
    if user.kind != "staff" or not user.has_role(TEACHER):
        return []
    from apps.academics.models import ClassArm
    from apps.academics.services import current_term

    term = current_term()
    if term is None:
        return []
    return list(
        ClassArm.objects.filter(
            form_teachers__teacher=user, form_teachers__session_id=term.session_id
        ).select_related("level")
    )


def _student_home(request):
    """A student's own profile card: no guardian contacts, no date of birth."""
    # Imported here, not at module level: apps.students and apps.academics import from
    # apps.accounts, so a top-level import would be circular.
    from apps.academics.services import current_term
    from apps.attendance.totals import term_totals
    from apps.comms.inbox import unread_count
    from apps.fees.checkout import payable_invoices
    from apps.fees.invoicing import outstanding_for
    from apps.students.models import Enrolment, Student

    term = current_term()
    student = Student.objects.filter(user=request.user).first()
    enrolment = None
    if student is not None and term is not None:
        enrolment = (
            Enrolment.objects.filter(
                student=student, session=term.session, status=Enrolment.Status.ACTIVE
            )
            .select_related("arm__level", "session")
            .first()
        )
    attendance = None
    if student is not None and term is not None:
        attendance = term_totals([student.pk], term).get(student.pk)
    return render(
        request,
        "students/portal_profile.html",
        {
            "attendance": attendance,
            "unread": unread_count(request.user),
            "student": student,
            "enrolment": enrolment,
            "term": term,
            "results_url": reverse("results:my_results"),
            "balance": outstanding_for(student) if student is not None else None,
            "fees_url": reverse("fees:my_fees") if student is not None else "",
            # Only when there is something to pay (own or a sibling's invoice).
            "pay_url": reverse("fees:pay")
            if student is not None and payable_invoices(student)
            else "",
        },
    )
