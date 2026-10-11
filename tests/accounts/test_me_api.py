import pytest
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
    }


def test_staff_me_roles_sorted_no_contact_details(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher", "proprietor"], by=u)
        client_a.force_login(u)
    data = client_a.get("/api/v1/me").json()
    assert data["roles"] == ["proprietor", "teacher"] and data["admission_number"] is None
    assert "email" not in data and "phone" not in data


def test_anonymous_401(client_a):
    r = client_a.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_post_not_allowed(client_a):
    with schema_context("school_a"):
        s = StudentFactory()
        client_a.force_login(s)
    assert client_a.post("/api/v1/me").status_code in {403, 405}


def test_school_b_session_cookie_rejected_on_school_a(client_a, client_b):
    with schema_context("school_b"):
        u = StudentFactory()
        client_b.force_login(u)
    assert client_b.get("/api/v1/me").status_code == 200
    client_a.cookies["sessionid"] = client_b.cookies["sessionid"].value
    assert client_a.get("/api/v1/me").status_code == 401
