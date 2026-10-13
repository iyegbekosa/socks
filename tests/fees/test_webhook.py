"""Paystack webhook (§4.5 steps 4–6): public, signed, enqueue-only; the DEBUG fake checkout."""

import json
import logging
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.fees import checkout, confirm, hooks, webhooks
from apps.fees.gateways import GatewayUnavailable
from apps.fees.gateways.fake import FakeGateway
from apps.fees.ledger import PlannedLine
from apps.fees.models import Allocation, Invoice, Payment, PaymentAttempt, Receipt
from apps.tenancy.models import School
from tests.fees.test_ledger import _invoice, _world

pytestmark = pytest.mark.django_db

PUBLIC_HOST = "localhost"
WEBHOOK = "/payments/webhook/paystack/"
SUBACCOUNT = "ACCT_school_a_test"
CALLBACK = "http://school-a.localhost/fees/pay/return/"
SECRET = "sk_test_webhook_secret"


@pytest.fixture(autouse=True)
def _env(settings):
    settings.DEBUG = True
    settings.ALLOWED_HOSTS = ["*"]
    settings.PAYMENT_GATEWAY = "apps.fees.gateways.fake.FakeGateway"
    settings.PAYSTACK_SECRET_KEY = SECRET
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    cache.clear()
    with mock.patch.object(hooks, "on_payment_confirmed"):
        yield
    cache.clear()


@pytest.fixture
def w(school_a):
    School.objects.filter(schema_name="school_a").update(paystack_subaccount_code=SUBACCOUNT)
    with schema_context("school_a"):
        world = _world()
        world.inv = _invoice(world.student, world.term1, 5_000_000)
    return world


def _start(w, amount=5_000_000) -> PaymentAttempt:
    with schema_context("school_a"):
        return checkout.start(w.student, [PlannedLine(w.inv.pk, amount)], callback_url=CALLBACK)


def _paid_at_gateway(reference: str, status: str = "success", amount_kobo=None) -> None:
    with schema_context("school_a"):
        FakeGateway.record_result(reference, status, amount_kobo=amount_kobo)


def _body(reference: str, event: str = "charge.success") -> bytes:
    return json.dumps(
        {
            "event": event,
            "data": {
                "reference": reference,
                "status": "success",
                "amount": 5_000_000,
                "currency": "NGN",
                "customer": {"email": "parent@example.com"},
            },
        }
    ).encode()


def _post(body: bytes, signature: str | None = None, host: str = PUBLIC_HOST, client=None):
    extra = {}
    if signature is not None:
        extra["HTTP_X_PAYSTACK_SIGNATURE"] = signature
    return (client or Client()).post(
        WEBHOOK, data=body, content_type="application/json", HTTP_HOST=host, **extra
    )


def _deliver(reference: str, event: str = "charge.success"):
    body = _body(reference, event)
    return _post(body, FakeGateway.sign(body))


def _counts():
    with schema_context("school_a"):
        return (
            Payment.objects.count(),
            Receipt.objects.count(),
            Allocation.objects.count(),
        )


def _status(reference: str) -> str:
    with schema_context("school_a"):
        return PaymentAttempt.objects.get(reference=reference).status


# --- the happy path -------------------------------------------------------------------------


def test_signed_charge_success_confirms_payment(w):
    attempt = _start(w)
    _paid_at_gateway(attempt.reference)
    resp = _deliver(attempt.reference)
    assert resp.status_code == 200
    assert not resp.cookies  # no session
    with schema_context("school_a"):
        attempt.refresh_from_db()
        assert attempt.status == PaymentAttempt.SUCCESS
        payment = Payment.objects.get()
        assert payment.attempt == attempt
        assert payment.method == "online"
        assert payment.gateway_reference == attempt.reference
        assert payment.amount_kobo == 5_000_000
        assert payment.student == w.student
        assert payment.unallocated_kobo == 0 and not payment.flagged
        w.inv.refresh_from_db()
        assert w.inv.status == Invoice.PAID
        assert w.inv.paid_kobo == 5_000_000
        assert Receipt.objects.filter(payment=payment).count() == 1
        assert attempt.raw_response["reference"] == attempt.reference
        assert "customer" not in attempt.raw_response
        assert "authorization" not in attempt.raw_response


def test_webhook_enqueues_on_payments_queue_with_schema_header(w):
    attempt = _start(w)
    with mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue:
        assert _deliver(attempt.reference).status_code == 200
    enqueue.assert_called_once_with(
        args=[attempt.reference], queue="payments", headers={"schema_name": "school_a"}
    )
    assert _counts() == (0, 0, 0)  # never confirms inline


# --- refusals -------------------------------------------------------------------------------


@pytest.mark.parametrize("signature", [None, "", "0" * 128, "nonsense"])
def test_bad_signature_is_401_and_queues_nothing(w, signature, caplog):
    attempt = _start(w)
    _paid_at_gateway(attempt.reference)
    body = _body(attempt.reference)
    with (
        mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue,
        caplog.at_level(logging.INFO),
    ):
        resp = _post(body, signature)
    assert resp.status_code == 401
    assert resp.content == b""
    enqueue.assert_not_called()
    assert _status(attempt.reference) == PaymentAttempt.PENDING
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "parent@example.com" not in caplog.text
    assert "charge.success" not in caplog.text


def test_tampered_body_is_401(w):
    attempt = _start(w)
    body = _body(attempt.reference)
    signature = FakeGateway.sign(body)
    with mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue:
        resp = _post(body.replace(b"5000000", b"9000000"), signature)
    assert resp.status_code == 401
    enqueue.assert_not_called()


def test_unknown_reference_is_200_and_queues_nothing(w, caplog):
    with (
        mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue,
        caplog.at_level(logging.INFO, logger="apps.fees.webhooks"),
    ):
        resp = _deliver("B2L-AAAAAAAAAAAAAAAAAAAA")
    assert resp.status_code == 200
    enqueue.assert_not_called()
    assert "unknown reference" in caplog.text


@pytest.mark.parametrize("event", ["charge.failed", "transfer.success", "subscription.create"])
def test_other_events_are_200_and_ignored(w, event):
    attempt = _start(w)
    _paid_at_gateway(attempt.reference)
    with mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue:
        assert _deliver(attempt.reference, event).status_code == 200
    enqueue.assert_not_called()
    assert _status(attempt.reference) == PaymentAttempt.PENDING


def test_signed_non_json_body_is_400_and_queues_nothing(w):
    payload = b"not json"
    with mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue:
        resp = _post(payload, FakeGateway.sign(payload))
    assert resp.status_code == 400
    enqueue.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"event": "charge.success"}',
        b'{"event": "charge.success", "data": 1}',
        b'{"event": "charge.success", "data": {"reference": 7}}',
        b'{"event": "charge.success", "data": {"reference": ""}}',
    ],
)
def test_signed_but_malformed_events_are_200_and_queue_nothing(w, payload):
    with mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue:
        resp = _post(payload, FakeGateway.sign(payload))
    assert resp.status_code == 200
    enqueue.assert_not_called()


def test_body_over_64kb_is_413(w):
    attempt = _start(w)
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": attempt.reference, "pad": "x" * 70_000}}
    ).encode()
    with (
        mock.patch.object(webhooks.process_attempt_task, "apply_async") as enqueue,
        mock.patch.object(FakeGateway, "check_signature") as check,
    ):
        resp = _post(body, FakeGateway.sign(body))
    assert resp.status_code == 413
    check.assert_not_called()
    enqueue.assert_not_called()


def test_get_is_405(w):
    assert Client().get(WEBHOOK, HTTP_HOST=PUBLIC_HOST).status_code == 405


def test_webhook_path_is_404_on_a_tenant_host(w, client_a):
    attempt = _start(w)
    _paid_at_gateway(attempt.reference)
    body = _body(attempt.reference)
    resp = client_a.post(
        WEBHOOK,
        data=body,
        content_type="application/json",
        HTTP_X_PAYSTACK_SIGNATURE=FakeGateway.sign(body),
    )
    assert resp.status_code == 404
    assert _counts() == (0, 0, 0)


def test_enqueue_failure_is_logged_by_type_and_still_200(w, caplog):
    attempt = _start(w)
    with (
        mock.patch.object(
            webhooks.process_attempt_task, "apply_async", side_effect=RuntimeError("broker gone")
        ),
        caplog.at_level(logging.WARNING, logger="apps.fees.webhooks"),
    ):
        assert _deliver(attempt.reference).status_code == 200
    assert "RuntimeError" in caplog.text
    assert "broker gone" not in caplog.text


def test_gateway_down_during_processing_keeps_attempt_pending(w):
    attempt = _start(w)
    with mock.patch.object(FakeGateway, "verify", side_effect=GatewayUnavailable("down")):
        assert _deliver(attempt.reference).status_code == 200
    assert _status(attempt.reference) == PaymentAttempt.PENDING
    assert _counts() == (0, 0, 0)


# --- Review Focus 1: duplicates and ordering ------------------------------------------------


def test_duplicate_and_concurrent_confirmation_credit_once(w):
    attempt = _start(w)
    _paid_at_gateway(attempt.reference)
    for _ in range(3):
        assert _deliver(attempt.reference).status_code == 200
    with schema_context("school_a"):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    assert _counts() == (1, 1, 1)
    with schema_context("school_a"):
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 5_000_000
        assert Allocation.objects.get().amount_kobo == 5_000_000


def test_return_page_before_webhook_shows_confirming_then_received(w, client_a):
    attempt = _start(w)
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    url = f"{reverse('fees:pay_return')}?reference={attempt.reference}"
    first = client_a.get(url)
    assert b"confirming" in first.content
    assert b"Payment received" not in first.content
    assert _counts() == (0, 0, 0)

    _paid_at_gateway(attempt.reference)
    assert _deliver(attempt.reference).status_code == 200
    second = client_a.get(url)
    assert b"Payment received" in second.content
    with schema_context("school_a"):
        receipt = Receipt.objects.get()
    assert reverse("fees:receipt_download", args=[receipt.pk]).encode() in second.content


def test_forged_return_url_never_changes_the_invoice(w, client_a):
    attempt = _start(w)
    _paid_at_gateway(attempt.reference)  # even with the gateway saying success
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    resp = client_a.get(
        f"{reverse('fees:pay_return')}?reference={attempt.reference}&status=success&trxref=x&paid=1"
    )
    assert resp.status_code == 200
    assert b"Payment received" not in resp.content
    assert _status(attempt.reference) == PaymentAttempt.PENDING
    assert _counts() == (0, 0, 0)
    with schema_context("school_a"):
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 0


# --- the DEBUG fake checkout ----------------------------------------------------------------


def _fake_url(reference: str) -> str:
    return reverse("fees:fake_checkout", args=[reference], urlconf="config.urls_tenant")


def test_fake_checkout_pay_button_runs_the_real_webhook_path(w, client_a):
    attempt = _start(w)
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    page = client_a.get(_fake_url(attempt.reference))
    assert b"Pay (test)" in page.content and b"Fail (test)" in page.content
    assert b"csrfmiddlewaretoken" in page.content
    with mock.patch.object(
        webhooks, "paystack_webhook", wraps=webhooks.paystack_webhook
    ) as handler:
        resp = client_a.post(_fake_url(attempt.reference), {"outcome": "success"})
    assert resp.status_code == 302
    assert resp["Location"] == f"{reverse('fees:pay_return')}?reference={attempt.reference}"
    handler.assert_called_once()
    assert _status(attempt.reference) == PaymentAttempt.SUCCESS
    assert _counts() == (1, 1, 1)
    assert b"Payment received" in client_a.get(resp["Location"]).content


def test_fake_checkout_signature_is_checked_for_real(w, client_a):
    attempt = _start(w)
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    with mock.patch.object(FakeGateway, "check_signature", return_value=False):
        resp = client_a.post(_fake_url(attempt.reference), {"outcome": "success"})
    assert resp.status_code == 302
    assert _status(attempt.reference) == PaymentAttempt.PENDING
    assert _counts() == (0, 0, 0)


def test_fake_checkout_fail_button_settles_the_attempt_as_failed(w, client_a):
    attempt = _start(w)
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    resp = client_a.post(_fake_url(attempt.reference), {"outcome": "failed"})
    assert resp.status_code == 302
    assert _counts() == (0, 0, 0)
    assert _status(attempt.reference) == PaymentAttempt.FAILED
    with schema_context("school_a"):
        assert FakeGateway.lookup(attempt.reference)["status"] == "failed"
    assert b"Payment was not completed" in client_a.get(resp["Location"]).content


def test_fake_checkout_post_refuses_bad_outcome_and_other_students(w, client_a):
    from tests.students.factories import StudentProfileFactory

    attempt = _start(w)
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    assert client_a.post(_fake_url(attempt.reference), {"outcome": "maybe"}).status_code == 400
    with schema_context("school_a"):
        other = StudentProfileFactory()
        client_a.force_login(other.user)
    assert client_a.post(_fake_url(attempt.reference), {"outcome": "success"}).status_code == 404
    assert _counts() == (0, 0, 0)


def test_fake_checkout_post_is_404_without_debug(w, client_a, settings):
    attempt = _start(w)
    with schema_context("school_a"):
        client_a.force_login(w.student.user)
    settings.DEBUG = False
    assert client_a.post(_fake_url(attempt.reference), {"outcome": "success"}).status_code == 404
    assert _counts() == (0, 0, 0)


# --- Ruling K: a late success on a failed/abandoned attempt is never dropped -----------------


def _set_status(reference: str, status: str) -> None:
    with schema_context("school_a"):
        PaymentAttempt.objects.filter(reference=reference).update(status=status)


def test_signed_success_on_abandoned_attempt_is_credited_late(w):
    attempt = _start(w)
    _set_status(attempt.reference, PaymentAttempt.ABANDONED)
    _paid_at_gateway(attempt.reference)
    assert _deliver(attempt.reference).status_code == 200
    assert _status(attempt.reference) == PaymentAttempt.SUCCESS
    assert _counts() == (1, 1, 1)
    with schema_context("school_a"):
        from apps.audit.models import AuditEvent

        event = AuditEvent.objects.get(action="fees.attempt_late_success")
        assert event.after == {"attempt_id": attempt.pk}
        assert event.object_id == str(attempt.pk)
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 5_000_000


def test_late_success_after_manual_payment_is_flagged_not_lost(w):
    from datetime import date

    from apps.fees import manual
    from tests.fees.factories import make_staff

    attempt = _start(w)
    _set_status(attempt.reference, PaymentAttempt.ABANDONED)
    with schema_context("school_a"):
        mp = manual.record(
            student=w.student,
            method="cash",
            amount_kobo=5_000_000,
            plan=[PlannedLine(w.inv.pk, 5_000_000)],
            paid_on=date.today(),
            reference_no="",
            by=w.bursar,
        )
        manual.approve(mp, by=make_staff("school_admin"))
    _paid_at_gateway(attempt.reference)
    assert _deliver(attempt.reference).status_code == 200
    assert _status(attempt.reference) == PaymentAttempt.SUCCESS
    with schema_context("school_a"):
        online = Payment.objects.get(attempt__reference=attempt.reference)
        assert online.flagged
        assert online.unallocated_kobo == 5_000_000
        assert not online.allocations.exists()
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == w.inv.total_kobo == 5_000_000
        assert Payment.objects.count() == 2


@pytest.mark.parametrize("gateway_status", ["failed", "abandoned", "pending"])
def test_failed_attempt_stays_unchanged_when_verify_is_not_success(w, gateway_status):
    attempt = _start(w)
    _set_status(attempt.reference, PaymentAttempt.FAILED)
    _paid_at_gateway(attempt.reference, gateway_status)
    with schema_context("school_a"):
        before = PaymentAttempt.objects.values().get(reference=attempt.reference)
    assert _deliver(attempt.reference).status_code == 200
    with schema_context("school_a"):
        assert PaymentAttempt.objects.values().get(reference=attempt.reference) == before
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FAILED
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 0
    assert _counts() == (0, 0, 0)


def test_route_to_the_wrong_school_credits_nobody(w, school_b):
    from apps.tenancy.models import PaymentRoute

    attempt = _start(w)
    _paid_at_gateway(attempt.reference)
    PaymentRoute.objects.filter(reference=attempt.reference).update(schema_name="school_b")
    assert _deliver(attempt.reference).status_code == 200
    assert _status(attempt.reference) == PaymentAttempt.PENDING
    assert _counts() == (0, 0, 0)
    with schema_context("school_b"):
        assert Payment.objects.count() == 0
