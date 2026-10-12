import pytest
from django_tenants.utils import schema_context

from tests.accounts.factories import StaffFactory
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def test_student_home_shows_profile_card(client_a, world):
    with schema_context("school_a"):
        p = StudentProfileFactory(
            first_name="Ada",
            last_name="Eze",
            guardian_name="Mrs Secretname",
            guardian_phone="+2348031234567",
            enrolment__arm=world.a,
            enrolment__session=world.session,
        )
        client_a.force_login(p.user)
    r = client_a.get("/")
    assert r.status_code == 200
    html = r.content.decode()
    assert "Ada Eze" in html and p.admission_number in html
    assert "JSS 1A" in html and "1st Term" in html and "2026/2027" in html
    assert "School A" in html
    assert 'href="/accounts/password/"' in html and "Change password" in html
    assert "Secretname" not in html and "8031234567" not in html and "Guardian" not in html


def test_staff_home_unchanged(client_a, world):
    with schema_context("school_a"):
        u = StaffFactory()
        from apps.accounts.roles import set_roles

        set_roles(u, ["teacher"], by=None)
        client_a.force_login(u)
    html = client_a.get("/").content.decode()
    assert "Students</a>" in html and "Change password" not in html
