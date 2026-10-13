"""Server-side verification and idempotent confirmation of online payments (§4.5 steps 5–6)."""

from datetime import UTC, date, datetime
from unittest import mock

import pytest
from celery.exceptions import Retry
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.core.logging import SECRET_ARG_TASKS
from apps.fees import checkout, confirm, hooks, ledger, manual, tasks
from apps.fees.gateways import GatewayUnavailable, VerifyResult
from apps.fees.gateways.fake import FakeGateway
from apps.fees.ledger import PlannedLine
from apps.fees.models import Allocation, Invoice, Payment, PaymentAttempt, Receipt
from apps.tenancy.models import School
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _invoice, _world

pytestmark = pytest.mark.django_db

SUBACCOUNT = "ACCT_school_a_test"
CALLBACK = "http://school-a.localhost/fees/pay/return/"
PAID_AT = datetime(2026, 10, 5, 10, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(settings):
    settings.DEBUG = True
    settings.PAYMENT_GATEWAY = "apps.fees.gateways.fake.FakeGateway"
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
        yield world


def _start(w, amount=5_000_000) -> PaymentAttempt:
    return checkout.start(w.student, [PlannedLine(w.inv.pk, amount)], callback_url=CALLBACK)


def _verify(status="success", amount=5_000_000, currency="NGN", subaccount=SUBACCOUNT, raw=None):
    return VerifyResult(
        status=status,
        amount_kobo=amount,
        currency=currency,
        subaccount=subaccount,
        paid_at=PAID_AT if status == "success" else None,
        raw=raw if raw is not None else {"status": status, "amount": amount},
    )


def _with_verify(result):
    if isinstance(result, Exception):
        return mock.patch.object(FakeGateway, "verify", side_effect=result)
    return mock.patch.object(FakeGateway, "verify", return_value=result)


def test_matching_success_confirms_with_planned_lines_and_paid_at(w):
    attempt = _start(w)
    raw = {"reference": attempt.reference, "status": "success", "amount": 5_000_000}
    with _with_verify(_verify(raw=raw)):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.SUCCESS
    assert attempt.raw_response == raw
    payment = Payment.objects.get()
    assert payment.attempt == attempt
    assert payment.method == "online"
    assert payment.paid_at == PAID_AT
    assert payment.recorded_by is None and payment.approved_by is None
    assert list(payment.allocations.values_list("invoice_id", "amount_kobo")) == [
        (w.inv.pk, 5_000_000)
    ]
    assert Receipt.objects.filter(payment=payment).count() == 1


def test_success_without_paid_at_uses_now(w):
    attempt = _start(w)
    result = VerifyResult("success", 5_000_000, "NGN", SUBACCOUNT, None, {})
    with _with_verify(result):
        confirm.process_attempt(attempt.reference)
    assert Payment.objects.get().paid_at is not None


@pytest.mark.parametrize(
    "result, field, reason",
    [
        (_verify(amount=4_999_999), "amount", "Amount mismatch"),
        (_verify(currency="USD"), "currency", "Currency mismatch"),
        (_verify(subaccount="ACCT_someone_else"), "subaccount", "Subaccount mismatch"),
        (_verify(subaccount=None), "subaccount", "Subaccount mismatch"),
    ],
)
def test_verify_mismatch_never_credits(w, result, field, reason):
    attempt = _start(w)
    with _with_verify(result):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FLAGGED
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.FLAGGED
    assert attempt.flag_reason == reason
    assert attempt.raw_response == result.raw
    assert Payment.objects.count() == 0
    w.inv.refresh_from_db()
    assert w.inv.paid_kobo == 0
    events = AuditEvent.objects.filter(action="fees.attempt_mismatch")
    assert events.count() == 1
    assert events.get().after == {"attempt_id": attempt.pk, "field": field}
    # Final: a later webhook or re-verification does not call the gateway again.
    with _with_verify(_verify()) as verify:
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FLAGGED
    verify.assert_not_called()
    assert Payment.objects.count() == 0


def test_verify_failed_while_webhook_claimed_success_never_credits(w):
    attempt = _start(w)
    with _with_verify(_verify(status="failed")):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FAILED
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.FAILED
    assert Payment.objects.count() == 0
    assert not AuditEvent.objects.filter(action="fees.attempt_mismatch").exists()


def test_mismatch_when_school_has_no_subaccount(w):
    attempt = _start(w)
    School.objects.filter(schema_name="school_a").update(paystack_subaccount_code="")
    with _with_verify(_verify(subaccount="")):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FLAGGED
    assert Payment.objects.count() == 0


def test_abandoned_is_recorded_and_pending_left_alone(w):
    first = _start(w)
    with _with_verify(_verify(status="abandoned")):
        assert confirm.process_attempt(first.reference) == PaymentAttempt.ABANDONED
    second = _start(w)
    for status in ("pending", "ongoing", "queued", "reversed"):
        with _with_verify(_verify(status=status)):
            assert confirm.process_attempt(second.reference) == PaymentAttempt.PENDING
    second.refresh_from_db()
    assert second.status == PaymentAttempt.PENDING
    assert Payment.objects.count() == 0


def test_final_attempt_returns_status_without_calling_gateway(w):
    attempt = _start(w)
    with _with_verify(_verify()):
        confirm.process_attempt(attempt.reference)
    with mock.patch.object(FakeGateway, "verify") as verify:
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    verify.assert_not_called()
    assert Payment.objects.count() == 1


def test_unknown_reference_in_this_school(w):
    with mock.patch.object(FakeGateway, "verify") as verify:
        assert confirm.process_attempt("B2L-AAAAAAAAAAAAAAAAAAAA") == confirm.MISSING
    verify.assert_not_called()


def test_verify_runs_outside_any_transaction_and_attempt_is_locked(w):
    attempt = _start(w)
    depth = len(connection.atomic_blocks)
    seen = []

    def spy(self, reference):
        seen.append(len(connection.atomic_blocks))
        return _verify()

    with (
        mock.patch.object(FakeGateway, "verify", spy),
        CaptureQueriesContext(connection) as ctx,
    ):
        confirm.process_attempt(attempt.reference)
    assert seen == [depth]
    locked = [
        q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"] and "attempt" in q["sql"]
    ]
    assert locked and '"fees_paymentattempt"' in locked[0]


def test_attempt_finalised_while_verifying_is_not_credited_twice(w):
    """Another worker confirms between our unlocked read and our lock: re-check wins."""
    attempt = _start(w)

    def racing_verify(self, reference):
        # The other worker gets there first (same process; sequential stand-in for a race).
        with mock.patch.object(FakeGateway, "verify", return_value=_verify()):
            confirm.process_attempt(reference)
        return _verify()

    with mock.patch.object(FakeGateway, "verify", racing_verify):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    assert (Payment.objects.count(), Receipt.objects.count(), Allocation.objects.count()) == (
        1,
        1,
        1,
    )


def test_existing_payment_with_same_reference_is_returned(w):
    attempt = _start(w)
    Payment.objects.create(
        student=w.student,
        amount_kobo=5_000_000,
        method="online",
        gateway_reference=attempt.reference,
        paid_at=PAID_AT,
    )
    with _with_verify(_verify()):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    assert Payment.objects.count() == 1
    w.inv.refresh_from_db()
    assert w.inv.paid_kobo == 0  # the existing payment is returned, nothing allocated twice


def test_integrity_error_path_returns_existing_payment(w):
    attempt = _start(w)
    existing = Payment.objects.create(
        student=w.student,
        amount_kobo=5_000_000,
        method="online",
        gateway_reference=attempt.reference,
        paid_at=PAID_AT,
    )
    with (
        _with_verify(_verify()),
        mock.patch.object(ledger, "_existing_payment", return_value=None),
    ):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    assert list(Payment.objects.all()) == [existing]
    assert Receipt.objects.count() == 0 and Allocation.objects.count() == 0
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.SUCCESS


def test_overpayment_after_manual_payment_is_flagged_not_lost(w):
    attempt = _start(w)  # pending for the full ₦50,000
    # The bursar records ₦50,000 (at the approval threshold) and an admin approves it.
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
    w.inv.refresh_from_db()
    assert w.inv.paid_kobo == 5_000_000
    with _with_verify(_verify()):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.SUCCESS
    online = Payment.objects.get(attempt=attempt)
    assert online.unallocated_kobo == 5_000_000
    assert online.flagged
    assert not online.allocations.exists()
    w.inv.refresh_from_db()
    assert w.inv.paid_kobo == w.inv.total_kobo == 5_000_000
    assert w.inv.status == Invoice.PAID
    assert Receipt.objects.filter(payment=online).count() == 1


@pytest.mark.parametrize("previous", [PaymentAttempt.FAILED, PaymentAttempt.ABANDONED])
def test_late_success_with_mismatch_is_flagged_not_credited(w, previous):
    attempt = _start(w)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(status=previous)
    with _with_verify(_verify(amount=1)):
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FLAGGED
    assert Payment.objects.count() == 0
    assert AuditEvent.objects.get(action="fees.attempt_mismatch").after == {
        "attempt_id": attempt.pk,
        "field": "amount",
    }
    assert not AuditEvent.objects.filter(action="fees.attempt_late_success").exists()


def test_failed_attempt_is_not_rewritten_as_abandoned(w):
    attempt = _start(w)
    PaymentAttempt.objects.filter(pk=attempt.pk).update(status=PaymentAttempt.FAILED)
    with _with_verify(_verify(status="abandoned")) as verify:
        assert confirm.process_attempt(attempt.reference) == PaymentAttempt.FAILED
    verify.assert_called_once()
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.FAILED
    assert attempt.raw_response == {}


def test_success_on_pending_attempt_has_no_late_audit(w):
    attempt = _start(w)
    with _with_verify(_verify()):
        confirm.process_attempt(attempt.reference)
    assert not AuditEvent.objects.filter(action="fees.attempt_late_success").exists()


# --- the task -------------------------------------------------------------------------------


def test_task_is_on_payments_queue_with_redacted_args():
    assert tasks.process_attempt_task.queue == "payments"
    assert tasks.process_attempt_task.max_retries == 3
    assert "apps.fees.tasks.process_attempt_task" in SECRET_ARG_TASKS


def test_task_runs_in_the_school_named_by_the_header(w):
    attempt = _start(w)
    FakeGateway.record_result(attempt.reference, "success")
    with schema_context("public"):
        tasks.process_attempt_task.apply_async(
            args=[attempt.reference], queue="payments", headers={"schema_name": "school_a"}
        )
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.SUCCESS


@pytest.mark.parametrize("retries, countdown", [(0, 60), (1, 300), (2, 900)])
def test_gateway_unavailable_retries_with_backoff_and_schema(w, retries, countdown):
    attempt = _start(w)
    captured = {}

    def fake_retry(self, args=None, kwargs=None, exc=None, throw=True, eta=None,
                   countdown=None, max_retries=None, **options):  # fmt: skip
        captured.update(countdown=countdown, headers=options.get("headers"))
        raise Retry()

    task = tasks.process_attempt_task
    # A worker-side request as Celery builds it: the schema arrives as a message header.
    task.push_request(retries=retries, headers={"schema_name": "school_a"})
    try:
        with (
            _with_verify(GatewayUnavailable("down")),
            mock.patch("celery.app.task.Task.retry", fake_retry),
            pytest.raises(Retry),
        ):
            task.run(attempt.reference)
    finally:
        task.pop_request()
    assert captured == {"countdown": countdown, "headers": {"schema_name": "school_a"}}
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.PENDING
    assert Payment.objects.count() == 0


def test_gateway_unavailable_via_apply_async_raises_retry(w):
    attempt = _start(w)
    with _with_verify(GatewayUnavailable("down")), schema_context("public"):
        with pytest.raises(Retry):
            tasks.process_attempt_task.apply_async(
                args=[attempt.reference], queue="payments", headers={"schema_name": "school_a"}
            )
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.PENDING


def test_exhausted_retries_raise_and_leave_attempt_pending(w):
    attempt = _start(w)
    with _with_verify(GatewayUnavailable("down")), schema_context("public"):
        with pytest.raises(GatewayUnavailable):
            tasks.process_attempt_task.apply(
                args=[attempt.reference], retries=3, headers={"schema_name": "school_a"}
            ).get()
    attempt.refresh_from_db()
    assert attempt.status == PaymentAttempt.PENDING
    assert Payment.objects.count() == 0
