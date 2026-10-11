"""Student profile factory: User + Student + Enrolment, plus the academics rows they need."""

from datetime import date

import factory

from apps.academics.models import AcademicSession, ClassArm, ClassLevel
from apps.students.models import Enrolment, Student
from tests.accounts.factories import StudentFactory


def default_session() -> AcademicSession:
    session, _ = AcademicSession.objects.get_or_create(
        name="2026/2027",
        defaults={"start_date": date(2026, 9, 1), "end_date": date(2027, 7, 31)},
    )
    return session


def default_arm() -> ClassArm:
    level, _ = ClassLevel.objects.get_or_create(
        name="JSS 1", defaults={"section": "jss", "order": 9}
    )
    arm, _ = ClassArm.objects.get_or_create(level=level, suffix="A")
    return arm


class StudentProfileFactory(factory.django.DjangoModelFactory):
    """Creates the student's login, profile and an active enrolment.

    Override the class with ``enrolment__arm=...`` / ``enrolment__session=...``.
    """

    class Meta:
        model = Student
        skip_postgeneration_save = True

    user = factory.SubFactory(StudentFactory)
    first_name = factory.SelfAttribute("user.first_name")
    last_name = factory.SelfAttribute("user.last_name")
    sex = "F"
    date_of_birth = date(2014, 3, 1)
    status = Student.Status.ACTIVE
    guardian_name = factory.Faker("name")
    guardian_relationship = "mother"
    guardian_phone = "+2348031234567"
    guardian_email = ""
    consent_method = "paper_form"

    @factory.post_generation
    def enrolment(self, create, extracted, **kwargs):
        if not create:
            return
        Enrolment.objects.create(
            student=self,
            arm=kwargs.get("arm") or default_arm(),
            session=kwargs.get("session") or default_session(),
            status=kwargs.get("status", Enrolment.Status.ACTIVE),
        )
