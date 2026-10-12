import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

from apps.academics.models import FormTeacher
from apps.accounts.roles import set_roles
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/students"


def admin_in(client):
    u = StaffFactory()
    set_roles(u, ["school_admin"], by=None)
    login_verified(client, u)
    return u


@pytest.fixture
def pupils(world):
    with schema_context("school_a"):
        ada = StudentProfileFactory(
            first_name="Ada",
            last_name="Eze",
            enrolment__arm=world.a,
            enrolment__session=world.session,
        )
        bayo = StudentProfileFactory(
            first_name="Bayo",
            last_name="Obi",
            enrolment__arm=world.b,
            enrolment__session=world.session,
        )
    return ada, bayo


def test_admin_lists_students_with_exact_fields(client_a, world, pupils):
    ada, bayo = pupils
    with schema_context("school_a"):
        admin_in(client_a)
    r = client_a.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    items = {i["full_name"]: i for i in body["results"]}
    assert items["Ada Eze"] == {
        "id": ada.pk,
        "admission_number": ada.admission_number,
        "full_name": "Ada Eze",
        "class": "JSS 1A",
    }
    assert items["Bayo Obi"]["class"] == "JSS 2 Gold"
    text = r.content.decode()
    assert "date_of_birth" not in text and "guardian" not in text and "2014" not in text


def test_filters(client_a, world, pupils):
    ada, bayo = pupils
    with schema_context("school_a"):
        admin_in(client_a)
    r = client_a.get(URL, {"class_arm": world.b.pk}).json()
    assert [i["full_name"] for i in r["results"]] == ["Bayo Obi"]
    r = client_a.get(URL, {"q": ada.admission_number}).json()
    assert [i["full_name"] for i in r["results"]] == ["Ada Eze"]
    r = client_a.get(URL, {"q": "obi"}).json()
    assert [i["full_name"] for i in r["results"]] == ["Bayo Obi"]
    bad = client_a.get(URL, {"class_arm": "x"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_page_size_capped_at_100(client_a, world, pupils):
    with schema_context("school_a"):
        admin_in(client_a)
    body = client_a.get(URL, {"page_size": 1000}).json()
    assert len(body["results"]) == 2
    body = client_a.get(URL, {"page_size": 1}).json()
    assert len(body["results"]) == 1 and body["next"]
    from apps.api.views import StudentPagination

    assert StudentPagination.max_page_size == 100 and StudentPagination.page_size == 50


def test_teacher_sees_only_own_arm_and_other_arm_filter_is_empty(client_a, world, pupils):
    with schema_context("school_a"):
        t = StaffFactory()
        set_roles(t, ["teacher"], by=None)
        FormTeacher.objects.create(arm=world.a, session=world.session, teacher=t)
        client_a.force_login(t)
    assert [i["full_name"] for i in client_a.get(URL).json()["results"]] == ["Ada Eze"]
    other = client_a.get(URL, {"class_arm": world.b.pk})
    assert other.status_code == 200 and other.json()["results"] == []


def test_student_forbidden_and_anonymous_401(client_a, world, pupils):
    assert client_a.get(URL).status_code == 401
    with schema_context("school_a"):
        client_a.force_login(StudentFactory())
    r = client_a.get(URL)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_query_count_is_bounded(client_a, world):
    with schema_context("school_a"):
        admin_in(client_a)
        for _ in range(3):
            StudentProfileFactory(enrolment__arm=world.a, enrolment__session=world.session)
    client_a.get(URL)  # warm caches
    with CaptureQueriesContext(connection) as few:
        client_a.get(URL)
    with schema_context("school_a"):
        for _ in range(10):
            StudentProfileFactory(enrolment__arm=world.b, enrolment__session=world.session)
    with CaptureQueriesContext(connection) as many:
        r = client_a.get(URL)
    assert r.json()["count"] == 13
    assert len(many) == len(few)
