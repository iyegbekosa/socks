import pytest
from django.contrib.sessions.models import Session
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from tests.accounts.factories import StaffFactory, StudentFactory

pytestmark = pytest.mark.django_db


def test_route_name():
    assert reverse("api:me") == "/api/v1/me"


def test_student_me(client_a):
    with schema_context("school_a"):
        s = StudentFactory(first_name="Ngozi", last_name="Eze")
        client_a.force_login(s)
    r = client_a.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json() == {
        "id": s.pk,
        "kind": "student",
        "full_name": "Ngozi Eze",
        "roles": [],
        "admission_number": s.admission_number,
        "school": {"name": "School A"},
        "current_class": None,
    }


def test_staff_me_roles_sorted_no_contact_details(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher", "proprietor"], by=u)
        client_a.force_login(u)
    data = client_a.get("/api/v1/me").json()
    assert data["roles"] == ["proprietor", "teacher"] and data["admission_number"] is None
    assert "current_class" not in data
    assert "email" not in data and "phone" not in data


def test_anonymous_401(client_a):
    r = client_a.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_post_not_allowed(client_a):
    with schema_context("school_a"):
        s = StudentFactory()
        client_a.force_login(s)
    assert client_a.post("/api/v1/me").status_code == 405


def test_post_without_csrf_token_is_403(school_a):
    from django_tenants.test.client import TenantClient

    c = TenantClient(school_a, enforce_csrf_checks=True)
    with schema_context("school_a"):
        c.force_login(StudentFactory())
    assert c.post("/api/v1/me").status_code == 403


def test_school_b_session_cookie_rejected_on_school_a(client_a, client_b):
    with schema_context("school_b"):
        u = StudentFactory()
        client_b.force_login(u)
    assert client_b.get("/api/v1/me").status_code == 200
    client_a.cookies["sessionid"] = client_b.cookies["sessionid"].value
    assert client_a.get("/api/v1/me").status_code == 401
    key = client_b.cookies["sessionid"].value
    with schema_context("school_b"):
        assert Session.objects.filter(session_key=key).exists()
    with schema_context("school_a"):
        assert not Session.objects.filter(session_key=key).exists()


def test_student_me_has_current_class(client_a, world):
    from tests.students.factories import StudentProfileFactory

    with schema_context("school_a"):
        p = StudentProfileFactory(enrolment__arm=world.a, enrolment__session=world.session)
        client_a.force_login(p.user)
    data = client_a.get("/api/v1/me").json()
    assert data["current_class"] == {"arm": "JSS 1A", "session": "2026/2027"}


def test_student_me_class_null_without_current_enrolment(client_a, world):
    from datetime import date

    from apps.academics.models import AcademicSession
    from tests.students.factories import StudentProfileFactory

    with schema_context("school_a"):
        old = AcademicSession.objects.create(
            name="2020/2021", start_date=date(2020, 9, 1), end_date=date(2021, 7, 1)
        )
        p = StudentProfileFactory(enrolment__arm=world.a, enrolment__session=old)
        client_a.force_login(p.user)
    assert client_a.get("/api/v1/me").json()["current_class"] is None
