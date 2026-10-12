"""POST /api/v1/payments/manual (UC-13)."""

import json
from unittest import mock

import pytest
from django_tenants.utils import schema_context

from apps.fees import hooks
from apps.fees.models import ManualPayment, Payment
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _invoice, _world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/payments/manual"


@pytest.fixture(autouse=True)
def _no_receipt_pdf():
    with mock.patch.object(hooks, "on_payment_confirmed"):
        yield


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = _world()
        world.inv = _invoice(world.student, world.term1, 10_000_000)
        yield world


def body(w, amount=2_000_000, **over):
    data = {
        "student": w.student.pk,
        "method": "cash",
        "amount_kobo": amount,
        "invoices": [{"invoice": w.inv.pk, "amount_kobo": amount}],
        "paid_on": "2026-10-01",
        "reference_no": "",
    }
    data.update(over)
    return data


def post(client, data):
    return client.post(URL, json.dumps(data), content_type="application/json")


def test_below_threshold_confirms(client_a, w):
    login_verified(client_a, w.bursar)
    r = post(client_a, body(w))
    assert r.status_code == 201, r.content
    data = r.json()
    with schema_context("school_a"):
        mp = ManualPayment.objects.get()
        payment = Payment.objects.get()
    assert data["id"] == mp.pk and data["status"] == "approved"
    assert data["payment"]["id"] == payment.pk
    assert data["payment"]["amount_kobo"] == 2_000_000
    assert data["payment"]["amount_display"] == "₦20,000.00"
    assert data["payment"]["receipt_number"].startswith("GFS/RCT/")


def test_above_threshold_is_pending(client_a, w):
    login_verified(client_a, w.bursar)
    r = post(
        client_a,
        body(w, 6_000_000, method="transfer", reference_no="TRF-9", student=str(w.student.pk)),
    )
    assert r.status_code == 201, r.content
    assert r.json()["status"] == "pending_approval" and r.json()["payment"] is None
    with schema_context("school_a"):
        assert not Payment.objects.exists()


@pytest.mark.parametrize(
    "over, field",
    [
        ({"method": "teller"}, "reference_no"),
        ({"paid_on": "2999-01-01"}, "paid_on"),
        ({"paid_on": "01/10/2026"}, "paid_on"),
        ({"amount_kobo": 2_500_000}, "invoices"),
        ({"amount_kobo": "2000000"}, "amount_kobo"),
        ({"amount_kobo": True}, "amount_kobo"),
        ({"method": "bitcoin"}, "method"),
        ({"invoices": "x"}, "invoices"),
        ({"invoices": [{"invoice": "abc", "amount_kobo": 1}]}, "invoices.0.invoice"),
        ({"student": "1e3"}, "student"),
    ],
)
def test_validation_errors(client_a, w, over, field):
    login_verified(client_a, w.bursar)
    r = post(client_a, body(w, **over))
    assert r.status_code == 400, r.content
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert field in {d["field"] for d in err["details"]}
    with schema_context("school_a"):
        assert not ManualPayment.objects.exists()


def test_other_students_invoice_is_a_validation_error(client_a, w):
    with schema_context("school_a"):
        other = _invoice(StudentProfileFactory(), w.term1, 5_000_000)
    login_verified(client_a, w.bursar)
    r = post(client_a, body(w, invoices=[{"invoice": other.pk, "amount_kobo": 2_000_000}]))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_ids_are_404(client_a, w):
    login_verified(client_a, w.bursar)
    r = post(client_a, body(w, student=999_999))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = post(client_a, body(w, invoices=[{"invoice": 999_999, "amount_kobo": 2_000_000}]))
    assert r.status_code == 404


def test_only_fee_managers(client_a, w):
    with schema_context("school_a"):
        proprietor = make_staff("proprietor")
        teacher = make_staff("teacher")
        admin = make_staff("school_admin")
    for user in (proprietor, teacher, w.student.user):
        client_a.force_login(user)
        r = post(client_a, body(w))
        assert r.status_code == 403, user
        assert r.json()["error"]["code"] == "FORBIDDEN"
    client_a.logout()
    assert post(client_a, body(w)).status_code == 401
    login_verified(client_a, admin)
    assert post(client_a, body(w)).status_code == 201
