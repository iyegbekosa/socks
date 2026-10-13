"""Gateway adapter: Paystack client over httpx.MockTransport (no network) and the fake gateway."""

import hashlib
import hmac
import json
import logging

import httpx
import pytest
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django_tenants.utils import schema_context

from apps.fees.gateways import (
    RESOLVE_REJECTED_MESSAGE,
    Bank,
    GatewayRejected,
    GatewayUnavailable,
    get_gateway,
)
from apps.fees.gateways.fake import FakeGateway
from apps.fees.gateways.paystack import PaystackGateway

KEY = "sk_test_secret"
REF = "B2L-ABCDEFGHIJKLMNOPQRST"


@pytest.fixture(autouse=True)
def _key(settings):
    settings.PAYSTACK_SECRET_KEY = KEY
    cache.clear()
    yield
    cache.clear()


class Recorder:
    """A MockTransport handler that records requests and answers from a script."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _gateway(*responses) -> tuple[PaystackGateway, Recorder]:
    rec = Recorder(*responses)
    return PaystackGateway(client=httpx.Client(transport=httpx.MockTransport(rec))), rec


def _ok(data, status=200):
    return httpx.Response(status, json={"status": True, "message": "ok", "data": data})


def _verify_payload(**over):
    data = {
        "id": 42,
        "status": "success",
        "reference": REF,
        "amount": 1_500_000,
        "currency": "NGN",
        "paid_at": "2026-10-12T09:30:00.000Z",
        "customer": {"email": "parent@example.com", "phone": "+2348030000000"},
        "authorization": {"last4": "4081", "bin": "408408", "authorization_code": "AUTH_x"},
        "subaccount": {"subaccount_code": "ACCT_school", "business_name": "School A"},
        "metadata": {"schema": "school_a", "attempt_id": 7},
    }
    data.update(over)
    return data


# --- configuration ---------------------------------------------------------


def test_paystack_needs_a_secret_key(settings):
    settings.PAYSTACK_SECRET_KEY = ""
    with pytest.raises(ImproperlyConfigured):
        PaystackGateway()


def test_get_gateway_loads_the_configured_class(settings):
    settings.PAYMENT_GATEWAY = "apps.fees.gateways.paystack.PaystackGateway"
    assert isinstance(get_gateway(), PaystackGateway)


# --- initialize ------------------------------------------------------------


def test_initialize_sends_split_fields_and_returns_url():
    gw, rec = _gateway(_ok({"authorization_url": "https://checkout.paystack.com/abc"}))
    result = gw.initialize(
        email="parent@example.com",
        amount_kobo=1_500_000,
        reference=REF,
        subaccount="ACCT_school",
        callback_url="https://school-a.localhost/pay/return/",
        metadata={"schema": "school_a", "attempt_id": 7},
    )
    assert result.authorization_url == "https://checkout.paystack.com/abc"
    (req,) = rec.requests
    assert req.method == "POST"
    assert str(req.url) == "https://api.paystack.co/transaction/initialize"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["bearer"] == "subaccount"
    assert body["currency"] == "NGN"
    assert body["amount"] == 1_500_000 and isinstance(body["amount"], int)
    assert body["reference"] == REF
    assert body["subaccount"] == "ACCT_school"
    assert body["callback_url"] == "https://school-a.localhost/pay/return/"
    assert body["metadata"] == {"schema": "school_a", "attempt_id": 7}


def test_initialize_status_false_is_unavailable():
    gw, _ = _gateway(httpx.Response(400, json={"status": False, "message": "Invalid key"}))
    with pytest.raises(GatewayUnavailable):
        gw.initialize(
            email="a@b.ng",
            amount_kobo=100,
            reference=REF,
            subaccount="ACCT_x",
            callback_url="https://x/",
            metadata={},
        )


# --- verify ----------------------------------------------------------------


def test_verify_maps_a_success_payload_and_drops_pii():
    gw, rec = _gateway(_ok(_verify_payload()))
    result = gw.verify(REF)
    assert str(rec.requests[0].url) == f"https://api.paystack.co/transaction/verify/{REF}"
    assert rec.requests[0].method == "GET"
    assert result.status == "success"
    assert result.amount_kobo == 1_500_000
    assert result.currency == "NGN"
    assert result.subaccount == "ACCT_school"
    assert result.paid_at is not None and result.paid_at.year == 2026
    assert result.paid_at.tzinfo is not None
    assert "customer" not in result.raw
    assert "authorization" not in result.raw
    assert result.raw["metadata"] == {"schema": "school_a", "attempt_id": 7}
    assert "parent@example.com" not in json.dumps(result.raw)


def test_verify_without_subaccount_gives_none():
    gw, _ = _gateway(_ok(_verify_payload(subaccount={}, paid_at=None, status="abandoned")))
    result = gw.verify(REF)
    assert result.subaccount is None
    assert result.paid_at is None
    assert result.status == "abandoned"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"status": False}),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": False, "message": "Transaction reference not found"}),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
    ],
    ids=["500", "502-html", "non-json", "status-false", "timeout", "connect-error"],
)
def test_verify_failures_are_unavailable(response):
    gw, _ = _gateway(response)
    with pytest.raises(GatewayUnavailable):
        gw.verify(REF)


def test_verify_quotes_the_reference_in_the_path():
    gw, rec = _gateway(_ok(_verify_payload()))
    gw.verify("../bank")
    assert rec.requests[0].url.raw_path == b"/transaction/verify/..%2Fbank"


# --- banks, resolve, subaccount ---------------------------------------------


def test_banks_are_cached_globally():
    gw, rec = _gateway(
        _ok([{"code": "058", "name": "GTBank"}, {"code": "044", "name": "Access Bank"}])
    )
    assert gw.banks() == [Bank(code="058", name="GTBank"), Bank(code="044", name="Access Bank")]
    req = rec.requests[0]
    assert req.url.path == "/bank"
    assert req.url.params["country"] == "nigeria" and req.url.params["currency"] == "NGN"
    # A second gateway (another school, another request) hits the cache, not the network.
    gw2, rec2 = _gateway()
    with schema_context("school_b"):
        assert gw2.banks() == [
            Bank(code="058", name="GTBank"),
            Bank(code="044", name="Access Bank"),
        ]
    assert rec2.requests == []


def test_banks_failure_is_unavailable_and_not_cached():
    gw, _ = _gateway(httpx.Response(503, text="down"))
    with pytest.raises(GatewayUnavailable):
        gw.banks()
    gw2, rec2 = _gateway(_ok([{"code": "058", "name": "GTBank"}]))
    assert gw2.banks() == [Bank(code="058", name="GTBank")]
    assert len(rec2.requests) == 1


def test_empty_bank_list_is_not_cached():
    gw, _ = _gateway(_ok([]))
    assert gw.banks() == []
    gw2, rec2 = _gateway(_ok([{"code": "058", "name": "GTBank"}]))
    assert gw2.banks() == [Bank(code="058", name="GTBank")]
    assert len(rec2.requests) == 1


def test_resolve_returns_the_account_name():
    gw, rec = _gateway(_ok({"account_number": "0123456789", "account_name": "SCHOOL A LTD"}))
    assert gw.resolve_account("0123456789", "058") == "SCHOOL A LTD"
    req = rec.requests[0]
    assert req.url.path == "/bank/resolve"
    assert req.url.params["account_number"] == "0123456789"
    assert req.url.params["bank_code"] == "058"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(422, json={"status": False, "message": "Could not resolve <b>x</b>"}),
        httpx.Response(400, text="nope"),
        httpx.Response(200, json={"status": False, "message": "Unknown bank code"}),
    ],
)
def test_resolve_rejection_has_a_fixed_safe_message(response):
    gw, _ = _gateway(response)
    with pytest.raises(GatewayRejected) as exc:
        gw.resolve_account("0123456789", "058")
    assert exc.value.message == RESOLVE_REJECTED_MESSAGE
    assert str(exc.value) == RESOLVE_REJECTED_MESSAGE


def test_resolve_server_error_is_unavailable():
    gw, _ = _gateway(httpx.Response(500, text="oops"))
    with pytest.raises(GatewayUnavailable):
        gw.resolve_account("0123456789", "058")


def test_save_subaccount_creates_with_post_then_updates_with_put():
    gw, rec = _gateway(_ok({"subaccount_code": "ACCT_new"}), _ok({"subaccount_code": "ACCT_new"}))
    code = gw.save_subaccount(
        code=None, business_name="School A", bank_code="058", account_number="0123456789"
    )
    assert code == "ACCT_new"
    create = rec.requests[0]
    assert create.method == "POST" and create.url.path == "/subaccount"
    assert json.loads(create.content) == {
        "business_name": "School A",
        "settlement_bank": "058",
        "account_number": "0123456789",
        "percentage_charge": 0,
    }
    assert (
        gw.save_subaccount(
            code="ACCT_new", business_name="School A", bank_code="044", account_number="9876543210"
        )
        == "ACCT_new"
    )
    update = rec.requests[1]
    assert update.method == "PUT" and update.url.path == "/subaccount/ACCT_new"
    assert json.loads(update.content)["settlement_bank"] == "044"


def test_save_subaccount_status_false_is_unavailable():
    gw, _ = _gateway(httpx.Response(400, json={"status": False, "message": "Bad"}))
    with pytest.raises(GatewayUnavailable):
        gw.save_subaccount(
            code=None, business_name="School A", bank_code="058", account_number="0123456789"
        )


# --- signatures ------------------------------------------------------------


def _sign(body: bytes, key: str = KEY) -> str:
    return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()


def test_check_signature():
    gw = PaystackGateway(client=httpx.Client(transport=httpx.MockTransport(Recorder())))
    body = b'{"event":"charge.success","data":{"reference":"B2L-X"}}'
    assert gw.check_signature(body, _sign(body)) is True
    assert gw.check_signature(body + b" ", _sign(body)) is False
    assert gw.check_signature(body, _sign(body, "other")) is False
    assert gw.check_signature(body, "") is False
    assert gw.check_signature(body, None) is False  # type: ignore[arg-type]
    assert gw.check_signature(body, "é" * 128) is False


# --- fake gateway ------------------------------------------------------------


def test_fake_gateway_refuses_without_debug(settings):
    settings.DEBUG = False
    with pytest.raises(ImproperlyConfigured):
        FakeGateway()


def test_fake_gateway_round_trip(settings):
    settings.DEBUG = True
    with schema_context("school_a"):
        gw = FakeGateway()
        result = gw.initialize(
            email="parent@example.com",
            amount_kobo=250_000,
            reference=REF,
            subaccount="ACCT_school",
            callback_url="https://school-a.localhost/fees/pay/return/",
            metadata={"schema": "school_a", "attempt_id": 1},
        )
        assert result.authorization_url == (f"https://school-a.localhost/fees/pay/test/{REF}/")
        pending = gw.verify(REF)
        assert pending.status == "pending"
        assert pending.amount_kobo == 250_000
        assert pending.subaccount == "ACCT_school"

        FakeGateway.record_result(REF, "success")
        done = gw.verify(REF)
        assert done.status == "success"
        assert done.amount_kobo == 250_000
        assert done.currency == "NGN"
        assert done.paid_at is not None

        FakeGateway.record_result(REF, "success", amount_kobo=1)
        assert gw.verify(REF).amount_kobo == 1

    # State is per school: the same reference in another schema is unknown.
    with schema_context("school_b"):
        assert FakeGateway().verify(REF).status == "pending"
        assert FakeGateway().verify(REF).amount_kobo == 0


def test_fake_gateway_relative_url_without_absolute_callback(settings):
    settings.DEBUG = True
    with schema_context("school_a"):
        result = FakeGateway().initialize(
            email="a@b.ng",
            amount_kobo=1,
            reference=REF,
            subaccount="ACCT_x",
            callback_url="/fees/pay/return/",
            metadata={},
        )
    assert result.authorization_url == f"/fees/pay/test/{REF}/"


def test_fake_gateway_signature_uses_fake_key_when_unset(settings):
    settings.DEBUG = True
    settings.PAYSTACK_SECRET_KEY = ""
    body = b'{"event":"charge.success"}'
    gw = FakeGateway()
    assert gw.check_signature(body, _sign(body, "fake")) is True
    assert gw.check_signature(body, _sign(body, KEY)) is False
    assert gw.check_signature(body, "") is False


def test_httpx_request_urls_are_not_logged():
    """Resolve URLs carry the account number; httpx's INFO request log must stay off."""
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
