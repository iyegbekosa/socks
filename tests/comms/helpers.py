"""A small school for the compose, history and messages API tests (P6 Task 4)."""

from types import SimpleNamespace

from apps.academics.models import AcademicSettings, ClassArm, Term
from apps.students.models import Student
from tests.fees.factories import make_staff, make_terms
from tests.students.factories import StudentProfileFactory, default_arm

MUM = "mum@example.com"
DAD = "chi.dad@example.com"


def make_world() -> SimpleNamespace:
    """JSS 1A holds siblings Ada and Bola (one shared guardian email), Dayo (no email) and a
    withdrawn student; JSS 1B holds Chi. One staff member per role."""
    t = make_terms()
    Term.objects.filter(pk=t.term1.pk).update(is_current=True)
    AcademicSettings.objects.update_or_create(pk=1, defaults={"admission_prefix": "GFS"})
    arm_a = default_arm()
    arm_b = ClassArm.objects.create(level=arm_a.level, suffix="B")

    def student(n, first, email="", arm=None):
        return StudentProfileFactory(
            first_name=first,
            guardian_email=email,
            user__admission_number=f"GFS/2026/{n:04d}",
            enrolment__arm=arm or arm_a,
        )

    a = student(1, "Ada", MUM)
    b = student(2, "Bola", MUM)
    c = student(3, "Chi", DAD, arm_b)
    d = student(4, "Dayo")
    gone = student(5, "Gone", "gone@example.com")
    Student.objects.filter(pk=gone.pk).update(status=Student.Status.WITHDRAWN)
    return SimpleNamespace(
        terms=t,
        term=t.term1,
        arm_a=arm_a,
        arm_b=arm_b,
        a=a,
        b=b,
        c=c,
        d=d,
        gone=gone,
        admin=make_staff("school_admin"),
        principal=make_staff("principal"),
        bursar=make_staff("bursar"),
        proprietor=make_staff("proprietor"),
        teacher=make_staff("teacher"),
    )


def give_debt(student, term, kobo: int):
    """An unpaid invoice of ``kobo`` for ``student`` in ``term``."""
    from tests.fees.test_ledger import _invoice

    return _invoice(student, term, kobo)
