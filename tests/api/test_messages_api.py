"""P6 Task 4: ``POST /api/v1/messages`` (``api:messages``)."""

import json

import pytest
from django.core import mail
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.comms.models import Message
from tests.accounts.helpers import login_verified
from tests.comms.helpers import MUM, give_debt, make_world

pytestmark = pytest.mark.django_db

URL = "/api/v1/messages"


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = make_world()
        mail.outbox.clear()
        yield world


def _post(client, body):
    return client.post(URL, data=json.dumps(body), content_type="application/json")


def _body(audience, **kw):
    return {"audience": audience, "subject": "Notice", "body": "Hello from {school_name}.", **kw}


def _count():
    with schema_context("school_a"):
        return Message.objects.count()


def _error(r):
    return r.json()["error"]


def test_url_name():
    assert reverse("api:messages") == URL


def test_admin_sends_to_an_arm(client_a, w, django_capture_on_commit_callbacks):
    login_verified(client_a, w.admin)
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(client_a, _body({"type": "arm", "id": w.arm_a.pk}))
    assert r.status_code == 201
    data = r.json()
    with schema_context("school_a"):
        msg = Message.objects.get()
        assert msg.audience_label == "JSS 1A"
        assert msg.created_by_id == w.admin.pk
    assert data == {"id": msg.pk, "email_count": 1, "inapp_count": 3}
    assert [(m.to, m.body) for m in mail.outbox] == [([MUM], "Hello from School A.")]


def test_principal_sends_to_school_level_and_students(client_a, w):
    login_verified(client_a, w.principal)
    r = _post(client_a, _body({"type": "school"}))
    assert (r.status_code, r.json()["inapp_count"]) == (201, 4)
    r = _post(client_a, _body({"type": "level", "id": w.arm_a.level_id}))
    assert (r.status_code, r.json()["inapp_count"]) == (201, 4)
    r = _post(client_a, _body({"type": "students", "ids": [w.a.pk, w.c.pk]}))
    assert r.json() == {"id": r.json()["id"], "email_count": 2, "inapp_count": 2}


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"audience": {"type": "school"}, "subject": "S", "body": "B", "extra": 1}, "extra"),
        (_body({"type": "school", "colour": "red"}), "audience.colour"),
        ({"audience": {"type": "school"}, "subject": "S"}, "body"),
        (_body({"type": "everyone"}), "audience.type"),
        (_body("school"), "audience"),
        (_body({"type": "arm"}), "audience.id"),
        (_body({"type": "arm", "id": "x"}), "audience.id"),
        (_body({"type": "students", "ids": []}), "audience.ids"),
        (_body({"type": "students", "ids": list(range(1, 52))}), "audience.ids"),
        (_body({"type": "debtors", "min_balance_kobo": -1}), "audience.min_balance_kobo"),
        (_body({"type": "school"}, subject=""), "subject"),
        (_body({"type": "school"}, subject="x" * 121), "subject"),
        (_body({"type": "school"}, body="x" * 2001), "body"),
        (_body({"type": "school"}, body="Hi {balance}"), "body"),
        (_body({"type": "school"}, body="Hi {nope}"), "body"),
    ],
)
def test_validation_errors(client_a, w, body, field):
    login_verified(client_a, w.admin)
    r = _post(client_a, body)
    assert r.status_code == 400, r.content
    err = _error(r)
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"][0]["field"] == field
    assert _count() == 0


def test_unknown_placeholder_message(client_a, w):
    login_verified(client_a, w.admin)
    r = _post(client_a, _body({"type": "school"}, body="Hi {nope}"))
    assert _error(r)["details"][0]["message"].startswith("Unknown placeholder {nope}. Use only:")


def test_empty_audience_and_withdrawn_students(client_a, w):
    login_verified(client_a, w.admin)
    with schema_context("school_a"):
        from apps.academics.models import ClassArm

        empty = ClassArm.objects.create(level=w.arm_a.level, suffix="C")
    r = _post(client_a, _body({"type": "arm", "id": empty.pk}))
    assert r.status_code == 400
    assert _error(r)["details"][0] == {
        "field": "audience",
        "message": "No students match this audience.",
    }
    r = _post(client_a, _body({"type": "students", "ids": [w.a.pk, w.gone.pk]}))
    assert r.status_code == 400
    assert _error(r)["details"][0]["message"] == f"Inactive students: {w.gone.pk}."
    assert _count() == 0


def test_unknown_ids_are_not_found(client_a, w):
    login_verified(client_a, w.admin)
    for audience in (
        {"type": "arm", "id": 7_000_123},
        {"type": "level", "id": 7_000_123},
        {"type": "debtors", "term": 7_000_123},
        {"type": "students", "ids": [w.a.pk, 7_000_123]},
    ):
        r = _post(client_a, _body(audience))
        assert r.status_code == 404, audience
        assert _error(r)["code"] == "NOT_FOUND"
    assert _count() == 0


def test_bursar_limited_to_debtors(client_a, w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        give_debt(w.a, w.term, 2_000_000)
        give_debt(w.c, w.term, 500_000)
    login_verified(client_a, w.bursar)
    for audience in (
        {"type": "school"},
        {"type": "level", "id": w.arm_a.level_id},
        {"type": "arm", "id": w.arm_a.pk},
        {"type": "students", "ids": [w.a.pk]},
    ):
        r = _post(client_a, _body(audience))
        assert r.status_code == 403, audience
        assert _error(r)["code"] == "FORBIDDEN"
    assert _count() == 0
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(
            client_a,
            _body({"type": "debtors", "term": w.term.pk, "min_balance_kobo": 1_000_000}),
        )
    assert r.status_code == 201
    assert r.json()["email_count"] == 1 and r.json()["inapp_count"] == 1
    with schema_context("school_a"):
        msg = Message.objects.get()
        assert msg.audience_label == "Debtors ≥ ₦10,000.00 — 1st Term 2026/2027"
    # The term defaults to the current one.
    r = _post(client_a, _body({"type": "debtors"}))
    assert (r.status_code, r.json()["inapp_count"]) == (201, 2)


@pytest.mark.parametrize("role", ["teacher", "proprietor"])
def test_other_staff_forbidden(client_a, w, role):
    client_a.force_login(getattr(w, role))
    r = _post(client_a, _body({"type": "school"}))
    assert r.status_code == 403
    assert _count() == 0


def test_student_and_anonymous_refused(client_a, w):
    r = _post(client_a, _body({"type": "school"}))
    assert r.status_code == 401
    client_a.force_login(w.a.user)
    assert _post(client_a, _body({"type": "school"})).status_code == 403
    assert _count() == 0


def test_get_not_allowed(client_a, w):
    login_verified(client_a, w.admin)
    assert client_a.get(URL).status_code == 405


def test_body_is_stripped_like_the_html_form(client_a, w):
    login_verified(client_a, w.admin)
    r = _post(client_a, _body({"type": "school"}, subject="  S  ", body="\n  Hello.  \n"))
    assert r.status_code == 201
    with schema_context("school_a"):
        msg = Message.objects.get()
        assert (msg.subject, msg.body) == ("S", "Hello.")
