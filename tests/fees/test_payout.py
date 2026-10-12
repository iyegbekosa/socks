"""School payout account page (FR-11): bank + account number become a Paystack subaccount."""

import json
from unittest import mock

import httpx
import pytest
from django.core.cache import cache
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.fees.gateways import RESOLVE_REJECTED_MESSAGE, UNAVAILABLE_MESSAGE
from apps.fees.gateways.fake import FakeGateway
from apps.fees.gateways.paystack import PaystackGateway
from apps.tenancy.models import School
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff

pytestmark = pytest.mark.django_db

ACCOUNT = "0123456789"
BANKS = [{"code": "058", "name": "GTBank"}, {"code": "044", "name": "Access Bank"}]


@pytest.fixture(autouse=True)
def _paystack(settings):
    settings.PAYSTACK_SECRET_KEY = "sk_test_secret"
    settings.PAYMENT_GATEWAY = "apps.fees.gateways.paystack.PaystackGateway"
    cache.clear()
    yield
    cache.clear()


class FakePaystack:
    """MockTransport handler that behaves like the Paystack endpoints the page uses."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.down = False
        self.resolve_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused")
        path = request.url.path
        if path == "/bank":
            return httpx.Response(200, json={"status": True, "data": BANKS})
        if path == "/bank/resolve":
            if self.resolve_status != 200:
                return httpx.Response(
                    self.resolve_status,
                    json={"status": False, "message": "Could not resolve account name"},
                )
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": {"account_number": ACCOUNT, "account_name": "SCHOOL A LIMITED"},
                },
            )
        if path.startswith("/subaccount"):
            return httpx.Response(
                200, json={"status": True, "data": {"subaccount_code": "ACCT_school_a"}}
            )
        return httpx.Response(404, json={"status": False})

    def calls(self, method: str, prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.startswith(prefix)]


@pytest.fixture
def paystack():
    handler = FakePaystack()

    def factory():
        return PaystackGateway(client=httpx.Client(transport=httpx.MockTransport(handler)))

    with mock.patch("apps.fees.views.payout.get_gateway", side_effect=factory):
        yield handler


def _login(client, role):
    with schema_context("school_a"):
        user = make_staff(role)
    if role in ("school_admin", "principal", "bursar"):
        login_verified(client, user)
    else:
        with schema_context("school_a"):
            client.force_login(user)
    return user


def _school() -> School:
    return School.objects.get(schema_name="school_a")


def _events(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action))


URL = "/fees/payout/"


def test_url_name():
    assert reverse("fees:payout", urlconf="config.urls_tenant") == URL


@pytest.mark.parametrize("role", ["bursar", "teacher", "proprietor", "principal"])
def test_payout_page_is_school_admin_only(client_a, paystack, role):
    _login(client_a, role)
    assert client_a.get(URL).status_code == 403
    assert client_a.post(URL, {"action": "check", "bank_code": "058"}).status_code == 403
    assert paystack.requests == []


def test_admin_resolve_then_confirm_saves_subaccount(client_a, paystack):
    _login(client_a, "school_admin")
    page = client_a.get(URL)
    assert page.status_code == 200
    assert b"GTBank" in page.content and b"Access Bank" in page.content
    assert _school().online_payments_ready is False

    checked = client_a.post(URL, {"action": "check", "bank_code": "058", "account_number": ACCOUNT})
    assert checked.status_code == 200
    assert b"SCHOOL A LIMITED" in checked.content
    assert b"Confirm" in checked.content
    assert paystack.calls("POST", "/subaccount") == []
    assert _school().paystack_subaccount_code == ""

    confirmed = client_a.post(URL, {"action": "confirm"})
    assert confirmed.status_code == 302
    school = _school()
    assert school.paystack_subaccount_code == "ACCT_school_a"
    assert school.payout_bank_name == "GTBank"
    assert school.payout_account_last4 == "6789"
    assert school.online_payments_ready is True
    (create,) = paystack.calls("POST", "/subaccount")
    body = json.loads(create.content)
    assert body["settlement_bank"] == "058"
    assert body["account_number"] == ACCOUNT
    assert body["business_name"] == school.name
    assert body["percentage_charge"] == 0

    (event,) = _events("fees.subaccount_saved")
    assert event.after == {"bank_code": "058", "account_last4": "6789", "updated": False}
    assert ACCOUNT not in json.dumps([event.before, event.after])
    assert "SCHOOL A LIMITED" not in json.dumps([event.before, event.after])
    # The full account number never stays in the session after saving.
    assert ACCOUNT not in json.dumps(dict(client_a.session))

    after = client_a.get(URL)
    assert b"GTBank" in after.content and b"6789" in after.content
    assert ACCOUNT.encode() not in after.content


def test_update_existing_subaccount_uses_put(client_a, paystack):
    School.objects.filter(schema_name="school_a").update(
        paystack_subaccount_code="ACCT_school_a",
        payout_bank_name="GTBank",
        payout_account_last4="6789",
    )
    _login(client_a, "school_admin")
    client_a.post(URL, {"action": "check", "bank_code": "044", "account_number": "9876543210"})
    assert client_a.post(URL, {"action": "confirm"}).status_code == 302
    assert paystack.calls("POST", "/subaccount") == []
    (update,) = paystack.calls("PUT", "/subaccount/ACCT_school_a")
    assert json.loads(update.content)["settlement_bank"] == "044"
    school = _school()
    assert school.payout_bank_name == "Access Bank"
    assert school.payout_account_last4 == "3210"
    (event,) = _events("fees.subaccount_saved")
    assert event.after == {"bank_code": "044", "account_last4": "3210", "updated": True}


def test_short_account_number_is_a_form_error(client_a, paystack):
    _login(client_a, "school_admin")
    resp = client_a.post(URL, {"action": "check", "bank_code": "058", "account_number": "12345"})
    assert resp.status_code == 200
    assert b"Enter the 10-digit account number." in resp.content
    assert paystack.calls("GET", "/bank/resolve") == []


def test_unknown_bank_is_a_form_error(client_a, paystack):
    _login(client_a, "school_admin")
    resp = client_a.post(URL, {"action": "check", "bank_code": "999", "account_number": ACCOUNT})
    assert resp.status_code == 200
    assert paystack.calls("GET", "/bank/resolve") == []


@pytest.mark.parametrize("status", [422, 400])
def test_resolve_rejection_shows_safe_message(client_a, paystack, status):
    paystack.resolve_status = status
    _login(client_a, "school_admin")
    resp = client_a.post(URL, {"action": "check", "bank_code": "058", "account_number": ACCOUNT})
    assert resp.status_code == 200
    assert RESOLVE_REJECTED_MESSAGE.encode() in resp.content
    assert b"Could not resolve account name" not in resp.content
    # Nothing pending, so confirming does nothing.
    client_a.post(URL, {"action": "confirm"})
    assert paystack.calls("POST", "/subaccount") == []
    assert _school().paystack_subaccount_code == ""


def test_gateway_down_shows_friendly_message(client_a, paystack):
    _login(client_a, "school_admin")
    paystack.down = True
    page = client_a.get(URL)
    assert page.status_code == 200
    assert UNAVAILABLE_MESSAGE.encode() in page.content
    assert UNAVAILABLE_MESSAGE == (
        "The payment provider is not responding. Please try again shortly."
    )


def test_gateway_down_at_check_and_confirm(client_a, paystack):
    _login(client_a, "school_admin")
    client_a.get(URL)  # banks now cached
    paystack.down = True
    resp = client_a.post(URL, {"action": "check", "bank_code": "058", "account_number": ACCOUNT})
    assert resp.status_code == 200
    assert UNAVAILABLE_MESSAGE.encode() in resp.content

    paystack.down = False
    client_a.post(URL, {"action": "check", "bank_code": "058", "account_number": ACCOUNT})
    paystack.down = True
    resp = client_a.post(URL, {"action": "confirm"})
    assert resp.status_code == 200
    assert UNAVAILABLE_MESSAGE.encode() in resp.content
    assert _school().paystack_subaccount_code == ""
    assert _events("fees.subaccount_saved") == []


def test_confirm_without_check_saves_nothing(client_a, paystack):
    _login(client_a, "school_admin")
    resp = client_a.post(URL, {"action": "confirm"})
    assert resp.status_code == 302
    assert paystack.calls("POST", "/subaccount") == []
    assert _school().paystack_subaccount_code == ""


def test_cancel_clears_the_pending_check(client_a, paystack):
    _login(client_a, "school_admin")
    client_a.post(URL, {"action": "check", "bank_code": "058", "account_number": ACCOUNT})
    assert client_a.post(URL, {"action": "cancel"}).status_code == 302
    client_a.post(URL, {"action": "confirm"})
    assert _school().paystack_subaccount_code == ""


# --- fake checkout route (DEBUG only) -----------------------------------------


REF = "B2L-ABCDEFGHIJKLMNOPQRST"


def test_fake_checkout_is_404_without_debug(client_a, settings):
    settings.DEBUG = False
    _login(client_a, "school_admin")
    assert client_a.get(f"/fees/pay/test/{REF}/").status_code == 404


def test_fake_checkout_shows_reference_and_amount(client_a, settings):
    settings.DEBUG = True
    _login(client_a, "school_admin")
    with schema_context("school_a"):
        url = (
            FakeGateway()
            .initialize(
                email="a@b.ng",
                amount_kobo=1_500_000,
                reference=REF,
                subaccount="ACCT_x",
                callback_url="/fees/",
                metadata={},
            )
            .authorization_url
        )
    assert url == reverse("fees:fake_checkout", args=[REF], urlconf="config.urls_tenant")
    resp = client_a.get(url)
    assert resp.status_code == 200
    assert REF.encode() in resp.content
    assert "₦15,000.00".encode() in resp.content
    assert client_a.get("/fees/pay/test/B2L-UNKNOWN/").status_code == 404
