from datetime import date
from types import SimpleNamespace

from django_tenants.utils import schema_context

from apps.results.models import AssessmentComponent, GradeBand
from apps.results.seeding import DEFAULT_COMPONENTS, seed_results


def reseed(school, schema: str = "school_a") -> None:
    with schema_context(schema):
        AssessmentComponent.objects.all().delete()
        GradeBand.objects.all().delete()
        seed_results(school)


def make_sheet_world() -> SimpleNamespace:
    """In the active schema: a current term, a JSS arm, a Maths offering with an assigned
    teacher, three active students, a withdrawn one and the default JSS components."""
    from apps.academics.models import Subject, SubjectOffering, Term
    from apps.accounts.roles import set_roles
    from apps.students.models import Enrolment
    from tests.accounts.factories import StaffFactory
    from tests.students.factories import StudentProfileFactory, default_arm, default_session

    Term.objects.filter(is_current=True).update(is_current=False)
    session = default_session()
    term1, _ = Term.objects.update_or_create(
        session=session,
        number=1,
        defaults={
            "start_date": date(2026, 9, 1),
            "end_date": date(2026, 12, 15),
            "is_current": True,
        },
    )
    term2, _ = Term.objects.get_or_create(
        session=session,
        number=2,
        defaults={"start_date": date(2027, 1, 5), "end_date": date(2027, 4, 1)},
    )
    arm = default_arm()
    if not AssessmentComponent.objects.filter(section="jss").exists():
        for order, (name, mx) in enumerate(DEFAULT_COMPONENTS, start=1):
            AssessmentComponent.objects.create(section="jss", name=name, max_score=mx, order=order)
    comps = list(AssessmentComponent.objects.filter(section="jss"))
    teacher = StaffFactory()
    set_roles(teacher, ["teacher"], by=None)
    subject, _ = Subject.objects.get_or_create(
        code="MTH", defaults={"name": "Maths", "sections": ["jss"]}
    )
    offering = SubjectOffering.objects.create(
        subject=subject, arm=arm, session=session, teacher=teacher
    )
    students = [StudentProfileFactory() for _ in range(3)]
    withdrawn = StudentProfileFactory(enrolment__status=Enrolment.Status.WITHDRAWN)
    return SimpleNamespace(
        session=session,
        term=term1,
        term2=term2,
        arm=arm,
        comps=comps,
        teacher=teacher,
        offering=offering,
        students=students,
        withdrawn=withdrawn,
    )


def make_card(student, term, arm, *, status: str = "published"):
    """In the active schema: the student's computed summary, the class result in ``status``
    and a stored report card row (no file is uploaded)."""
    from decimal import Decimal

    from django.utils import timezone

    from apps.results.models import ClassResult, ReportCard, ResultSummary

    ResultSummary.objects.update_or_create(
        student=student,
        term=term,
        defaults={
            "arm": arm,
            "subjects_count": 1,
            "total": Decimal("50"),
            "average": Decimal("50"),
            "position": 1,
            "out_of": 1,
            "computed_at": timezone.now(),
        },
    )
    ClassResult.objects.update_or_create(arm=arm, term=term, defaults={"status": status})
    return ReportCard.objects.create(
        student=student,
        term=term,
        arm=arm,
        file_name=f"report_cards/{term.session_id}/{term.pk}/{student.pk}.pdf",
        generated_at=timezone.now(),
    )


def accuracy_world() -> SimpleNamespace:
    """In the active schema: the Task 7 accuracy dataset (6 students, 3 subjects) entered and
    submitted for the current 1st term, plus a form teacher, a principal, a verified school
    admin, a proprietor and a teacher who is not the form teacher. Not yet computed."""
    from apps.academics.models import FormTeacher, Term
    from apps.accounts.roles import set_roles
    from tests.accounts.factories import StaffFactory
    from tests.results.test_accuracy import (
        SCORES,
        SUBJECTS,
        _enter,
        _jss_structure,
        _offerings,
        _submit,
        _term,
    )
    from tests.students.factories import StudentProfileFactory, default_arm, default_session

    def staff(*roles):
        user = StaffFactory()
        set_roles(user, list(roles), by=None)
        return user

    Term.objects.filter(is_current=True).update(is_current=False)
    session = default_session()
    term = _term(session, 1, current=True)
    term2 = _term(session, 2)
    arm = default_arm()
    comps = list(_jss_structure())
    teacher = StaffFactory(first_name="Ada", last_name="Okafor")
    set_roles(teacher, ["teacher"], by=None)
    offerings = _offerings(arm, session, teacher, SUBJECTS)
    students = {}
    for letter, rows in SCORES.items():
        st = StudentProfileFactory(first_name="Pupil", last_name=letter)
        students[letter] = st
        for name, values in zip(SUBJECTS, rows, strict=True):
            _enter(st, offerings[name], term, comps, values, teacher)
    _submit(offerings.values(), term)
    ft = staff("teacher")
    FormTeacher.objects.create(arm=arm, session=session, teacher=ft)
    admin = staff("school_admin")
    admin.is_verified = lambda: True
    return SimpleNamespace(
        session=session,
        term=term,
        term2=term2,
        arm=arm,
        comps=comps,
        teacher=teacher,
        offerings=offerings,
        students=students,
        ft=ft,
        principal=staff("principal"),
        admin=admin,
        proprietor=staff("proprietor"),
    )


def approve_and_compute(world) -> None:
    """Move the accuracy class to ``approved`` through ``approve_class`` and compute it, as
    the on-commit compute task would."""
    from apps.results.models import ClassResult
    from apps.results.workflow import approve_class, run_compute

    ClassResult.objects.update_or_create(
        arm=world.arm, term=world.term, defaults={"status": ClassResult.Status.REVIEWED}
    )
    approve_class(world.arm, world.term, by=world.principal)
    run_compute(world.arm, world.term)
