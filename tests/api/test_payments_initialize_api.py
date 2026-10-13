"""POST /api/v1/payments/initialize (§6.2): a student starts an online payment."""

import json
from unittest import mock

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.fees.gateways import GatewayUnavailable
from apps.fees.gateways.fake import FakeGateway
from apps.fees.models import FeeSettings, PaymentAttempt
from apps.tenancy.models import PaymentRoute
from tests.accounts.helpers import login_verified
from tests.fees import test_checkout as tc
from tests.fees.factories import make_staff
from tests.fees.test_checkout import BANK_NOTE, REFERENCE_RE, _cworld, make_ready

pytestmark = pytest.mark.django_db

URL = "/api/v1/payments/initialize"

# Shared fixtures from the checkout tests.
_fake_gateway = tc._fake_gateway
_no_receipt_pdf = tc._no_receipt_pdf
ready = tc.ready
w = tc.w


def post(client, data):
    return client.post(URL, json.dumps(data), content_type="application/json")


def body(*lines):
    return {"invoices": [{"invoice": inv.pk, "amount_kobo": amount} for inv, amount in lines]}


def test_url_name():
    assert reverse("api:payments_initialize") == URL


def test_student_starts_payment(w, client_a):
    client_a.force_login(w.student.user)
    r = post(client_a, body((w.inv, 10_000_000), (w.sib_inv, 5_000_000)))
    assert r.status_code == 201, r.content
    data = r.json()
    assert set(data) == {"reference", "authorization_url"}
    assert REFERENCE_RE.match(data["reference"])
    assert data["authorization_url"].endswith(
        reverse("fees:fake_checkout", args=[data["reference"]])
    )
    with schema_context("school_a"):
        attempt = PaymentAttempt.objects.get(reference=data["reference"])
        assert attempt.amount_kobo == 15_000_000 and attempt.student == w.student
    with schema_context("public"):
        assert PaymentRoute.objects.get(reference=data["reference"]).schema_name == "school_a"


@pytest.mark.parametrize("role", ["school_admin", "bursar", "teacher"])
def test_staff_are_forbidden(w, client_a, role):
    login_verified(client_a, make_staff(role))
    r = post(client_a, body((w.inv, 10_000_000)))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    with schema_context("school_a"):
        assert not PaymentAttempt.objects.exists()


def test_anonymous_is_refused(w, client_a):
    r = post(client_a, body((w.inv, 10_000_000)))
    assert r.status_code in (401, 403)


def test_gateway_down_is_503_with_bank_note(w, client_a):
    client_a.force_login(w.student.user)
    with mock.patch.object(FakeGateway, "initialize", side_effect=GatewayUnavailable("x")):
        r = post(client_a, body((w.inv, 10_000_000)))
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "GATEWAY_UNAVAILABLE"
    assert "Online payment is not available right now." in err["message"]
    assert BANK_NOTE in err["message"]
    with schema_context("school_a"):
        assert PaymentAttempt.objects.get().status == PaymentAttempt.FAILED


def test_not_ready_is_409(school_a, client_a):
    make_ready(code="")
    with schema_context("school_a"):
        world = _cworld()
        client_a.force_login(world.student.user)
    r = post(client_a, body((world.inv, 10_000_000)))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ONLINE_PAYMENTS_NOT_READY"
    with schema_context("school_a"):
        assert not PaymentAttempt.objects.exists()


def test_foreign_invoice_is_404(w, client_a):
    client_a.force_login(w.student.user)
    r = post(client_a, body((w.other_inv, 4_000_000)))
    assert r.status_code == 404
    r = post(client_a, {"invoices": [{"invoice": 999_999, "amount_kobo": 100}]})
    assert r.status_code == 404


@pytest.mark.parametrize(
    "data, field",
    [
        ([], "non_field_errors"),
        ({}, "invoices"),
        ({"invoices": []}, "invoices"),
        ({"invoices": [{"invoice": 1}]}, "invoices.0"),
        ({"invoices": [{"invoice": "x", "amount_kobo": 1}]}, "invoices.0.invoice"),
        ({"invoices": [{"invoice": 1, "amount_kobo": "100"}]}, "invoices.0.amount_kobo"),
        ({"invoices": [{"invoice": 1, "amount_kobo": True}]}, "invoices.0.amount_kobo"),
        ({"invoices": [], "extra": 1}, "extra"),
    ],
)
def test_bad_bodies_are_400(w, client_a, data, field):
    client_a.force_login(w.student.user)
    r = post(client_a, data)
    assert r.status_code == 400, r.content
    assert r.json()["error"]["details"][0]["field"] == field


def test_part_payment_rules_are_400(w, client_a):
    conf = FeeSettings.load()
    conf.allow_part_payment = False
    conf.save()
    client_a.force_login(w.student.user)
    r = post(client_a, body((w.inv, 5_000_000)))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    with schema_context("school_a"):
        assert not PaymentAttempt.objects.exists()
