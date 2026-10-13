"""Re-verification sweep and the reconciliation page (§4.5 step 7, §8.1)."""

import logging
from datetime import timedelta
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.fees import checkout, hooks, reconcile, tasks
from apps.fees.gateways import GatewayUnavailable
from apps.fees.gateways.fake import FakeGateway
from apps.fees.ledger import PlannedLine
from apps.fees.models import Payment, PaymentAttempt, Receipt
from apps.tenancy.models import School
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _confirm, _invoice, _world

pytestmark = pytest.mark.django_db

SUBACCOUNT = "ACCT_school_a_test"
CALLBACK = "http://school-a.localhost/fees/pay/return/"
NOTE = "Refunded the parent by bank transfer on 12 October"


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


def _attempt(w, minutes_old, amount=5_000_000, invoice=None) -> PaymentAttempt:
    invoice = invoice or w.inv
    a = checkout.start(w.student, [PlannedLine(invoice.pk, amount)], callback_url=CALLBACK)
    PaymentAttempt.objects.filter(pk=a.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes_old)
    )
    return a


def _succeed(a):
    FakeGateway.record_result(a.reference, "success", a.amount_kobo)


def _status(a):
    return PaymentAttempt.objects.get(pk=a.pk).status


# --- sweep ------------------------------------------------------------------------------


def test_fan_out_enqueues_active_ready_schools_only(school_a, school_b):
    School.objects.filter(schema_name="school_b").update(is_active=False)
    with mock.patch.object(tasks.reverify_school_task, "apply_async") as enqueue:
        tasks.reverify_all_schools()
    schemas = [c.kwargs["headers"]["schema_name"] for c in enqueue.call_args_list]
    assert "school_a" in schemas
    assert "school_b" not in schemas
    assert "public" not in schemas
    assert all(c.kwargs["queue"] == "payments" for c in enqueue.call_args_list)


def test_fan_out_skips_unprovisioned_schools(school_a, school_b):
    School.objects.filter(schema_name="school_b").update(provisioning_status="pending")
    with mock.patch.object(tasks.reverify_school_task, "apply_async") as enqueue:
        tasks.reverify_all_schools()
    schemas = [c.kwargs["headers"]["schema_name"] for c in enqueue.call_args_list]
    assert "school_b" not in schemas


def test_beat_schedule(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["fees-reverify"]
    assert entry["task"] == "apps.fees.tasks.reverify_all_schools"
    assert entry["schedule"] == 900.0
    assert entry["options"] == {"queue": "payments"}


def test_old_pending_success_is_confirmed_and_young_one_untouched(w):
    old = _attempt(w, 31)
    _succeed(old)
    young = _attempt(w, 10)
    _succeed(young)
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(old) == PaymentAttempt.SUCCESS
    assert Payment.objects.filter(attempt=old).exists()
    assert _status(young) == PaymentAttempt.PENDING


def test_stale_pending_that_fails_becomes_abandoned(w):
    a = _attempt(w, 25 * 60)
    FakeGateway.record_result(a.reference, "failed")
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.ABANDONED


def test_stale_pending_still_in_progress_becomes_abandoned(w):
    a = _attempt(w, 25 * 60)
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.ABANDONED


def test_pending_under_a_day_that_fails_is_failed_not_abandoned(w):
    a = _attempt(w, 40)
    FakeGateway.record_result(a.reference, "failed")
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.FAILED


def test_failed_is_never_downgraded_and_late_success_is_credited(w):
    a = _attempt(w, 25 * 60)
    PaymentAttempt.objects.filter(pk=a.pk).update(status="failed")
    FakeGateway.record_result(a.reference, "failed")
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.FAILED
    _succeed(a)
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.SUCCESS


def test_failed_older_than_a_day_is_not_rechecked(w):
    a = _attempt(w, 60)
    PaymentAttempt.objects.filter(pk=a.pk).update(
        status="failed", updated_at=timezone.now() - timedelta(hours=25)
    )
    _succeed(a)
    tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.FAILED


def test_cap_is_200_pending_first_oldest_first(w):
    refs = []
    for i in range(3):
        refs.append(_attempt(w, 60 + i).reference)
    failed = _attempt(w, 5)
    PaymentAttempt.objects.filter(pk=failed.pk).update(status="failed")
    seen = []
    with (
        mock.patch("apps.fees.reconcile.REVERIFY_CAP", 2),
        mock.patch(
            "apps.fees.reconcile.process_attempt", side_effect=lambda r, **k: seen.append(r) or "x"
        ),
    ):
        tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert seen == [refs[2], refs[1]]  # oldest first, failed one cut off


def test_gateway_errors_are_skipped_then_run_stops_after_three(w):
    for i in range(5):
        _attempt(w, 60 + i)
    with (
        mock.patch(
            "apps.fees.reconcile.process_attempt", side_effect=GatewayUnavailable("x")
        ) as pa,
    ):
        tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert pa.call_count == 3


def test_a_gateway_error_does_not_stop_the_next_attempt(w):
    first = _attempt(w, 90)
    second = _attempt(w, 60)
    calls = []

    def fake(ref, **kw):
        calls.append(ref)
        if ref == first.reference:
            raise GatewayUnavailable("x")
        return "success"

    with mock.patch("apps.fees.reconcile.process_attempt", side_effect=fake):
        tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert calls == [first.reference, second.reference]


def test_a_database_error_is_logged_by_type_and_the_sweep_goes_on(w, caplog):
    """M4: a deadlock-like DatabaseError on one attempt rolls back only that attempt."""
    first = _attempt(w, 90)
    second = _attempt(w, 60)
    _succeed(second)
    real = reconcile.process_attempt

    def fake(ref, **kw):
        if ref == first.reference:
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM fees_no_such_table_secret")
        return real(ref, **kw)

    with (
        mock.patch("apps.fees.reconcile.process_attempt", side_effect=fake),
        caplog.at_level(logging.WARNING),
    ):
        assert reconcile.reverify_school() == {"checked": 1, "errors": 1}
    assert _status(second) == PaymentAttempt.SUCCESS
    assert _status(first) == PaymentAttempt.PENDING
    assert "ProgrammingError" in caplog.text and "secret" not in caplog.text


# --- page -------------------------------------------------------------------------------


def _flagged_payment(w, over=2_000_000):
    _confirm(w, [(w.inv, 5_000_000)], 5_000_000 + over)
    return Payment.objects.get(flagged=True)


def test_page_lists_flagged_items_and_shows_buttons_to_managers(w, client_a):
    pay = _flagged_payment(w)
    a = _attempt(w, 60, invoice=_invoice(w.student, w.term2, 5_000_000))
    PaymentAttempt.objects.filter(pk=a.pk).update(status="flagged", flag_reason="Amount mismatch")
    Receipt.objects.filter(payment=pay).update(pdf_name="")
    login_verified(client_a, w.bursar)
    r = client_a.get(reverse("fees:reconcile"))
    html = r.content.decode()
    assert r.status_code == 200
    assert "₦20,000.00" in html
    assert pay.receipt.number in html
    assert a.reference in html and "Amount mismatch" in html
    assert w.student.admission_number in html
    assert "Mark resolved" in html and "Regenerate" in html
    assert "Re-check now" not in html  # the only attempt is flagged: read-only
    assert reverse("fees:manual_pending") in html


def test_proprietor_sees_page_without_buttons(w, client_a):
    _flagged_payment(w)
    a = _attempt(w, 60, invoice=_invoice(w.student, w.term2, 5_000_000))
    PaymentAttempt.objects.filter(pk=a.pk).update(status="flagged")
    client_a.force_login(make_staff("proprietor"))
    r = client_a.get(reverse("fees:reconcile"))
    html = r.content.decode()
    assert r.status_code == 200 and "₦20,000.00" in html
    assert "Mark resolved" not in html and "Re-check now" not in html
    assert a.reference in html


def test_teacher_gets_403(w, client_a):
    client_a.force_login(make_staff("teacher"))
    assert client_a.get(reverse("fees:reconcile")).status_code == 403
    pay = _flagged_payment(w)
    r = client_a.post(reverse("fees:payment_resolve", args=[pay.pk]), {"note": NOTE})
    assert r.status_code == 403


def test_mark_resolved_needs_a_note(w, client_a):
    pay = _flagged_payment(w)
    login_verified(client_a, w.bursar)
    url = reverse("fees:payment_resolve", args=[pay.pk])
    for note in ("", "   ", "x" * 501):
        r = client_a.post(url, {"note": note})
        assert r.status_code == 400
    pay.refresh_from_db()
    assert pay.flagged


def test_mark_resolved_clears_flag_and_audits_without_note(w, client_a):
    pay = _flagged_payment(w)
    login_verified(client_a, w.bursar)
    r = client_a.post(reverse("fees:payment_resolve", args=[pay.pk]), {"note": NOTE})
    assert r.status_code == 302
    pay.refresh_from_db()
    assert not pay.flagged and pay.unallocated_kobo == 2_000_000
    assert pay.resolution_note == NOTE
    assert pay.resolved_by == w.bursar and pay.resolved_at is not None
    ev = AuditEvent.objects.get(action="fees.payment_resolved")
    assert ev.after == {"payment_id": pay.pk, "unallocated_kobo": 2_000_000}
    assert NOTE not in str(ev.after) and NOTE not in str(ev.before)


def test_mark_resolved_on_unflagged_payment_is_rejected(w, client_a):
    _confirm(w, [(w.inv, 1_000_000)], 1_000_000)
    pay = Payment.objects.get()
    login_verified(client_a, w.bursar)
    r = client_a.post(reverse("fees:payment_resolve", args=[pay.pk]), {"note": NOTE})
    assert r.status_code == 409
    assert not AuditEvent.objects.filter(action="fees.payment_resolved").exists()


def test_proprietor_cannot_resolve(w, client_a):
    pay = _flagged_payment(w)
    client_a.force_login(make_staff("proprietor"))
    r = client_a.post(reverse("fees:payment_resolve", args=[pay.pk]), {"note": NOTE})
    assert r.status_code == 403
    pay.refresh_from_db()
    assert pay.flagged


def test_recheck_confirms_a_pending_attempt(w, client_a):
    a = _attempt(w, 5)
    _succeed(a)
    login_verified(client_a, w.bursar)
    r = client_a.post(reverse("fees:attempt_recheck", args=[a.pk]), follow=True)
    assert _status(a) == PaymentAttempt.SUCCESS
    assert "success" in r.content.decode().lower()


def test_recheck_gateway_down_is_friendly(w, client_a):
    a = _attempt(w, 5)
    login_verified(client_a, w.bursar)
    with mock.patch(
        "apps.fees.views.reconcile.process_attempt", side_effect=GatewayUnavailable("boom")
    ):
        r = client_a.post(reverse("fees:attempt_recheck", args=[a.pk]), follow=True)
    html = r.content.decode()
    assert r.status_code == 200 and "boom" not in html
    assert "could not reach" in html.lower()
    assert _status(a) == PaymentAttempt.PENDING


def test_recheck_proprietor_403_and_bad_id_404(w, client_a):
    a = _attempt(w, 5)
    client_a.force_login(make_staff("proprietor"))
    assert client_a.post(reverse("fees:attempt_recheck", args=[a.pk])).status_code == 403
    login_verified(client_a, w.bursar)
    assert client_a.post(reverse("fees:attempt_recheck", args=[999999])).status_code == 404


def test_nav_links_to_reconciliation(w, client_a):
    login_verified(client_a, w.bursar)
    assert reverse("fees:reconcile") in client_a.get(reverse("fees:structures")).content.decode()


def test_recheck_section_lists_open_attempts_only(w, client_a):
    pend = _attempt(w, 5)
    failed = _attempt(w, 60)
    PaymentAttempt.objects.filter(pk=failed.pk).update(status="failed")
    old_failed = _attempt(w, 60)
    PaymentAttempt.objects.filter(pk=old_failed.pk).update(
        status="failed", updated_at=timezone.now() - timedelta(hours=30)
    )
    login_verified(client_a, w.bursar)
    html = client_a.get(reverse("fees:reconcile")).content.decode()
    assert pend.reference in html and failed.reference in html
    assert old_failed.reference not in html
    assert reverse("fees:attempt_recheck", args=[pend.pk]) in html


def test_recheck_of_flagged_or_success_attempt_never_calls_gateway(w, client_a):
    a = _attempt(w, 60)
    login_verified(client_a, w.bursar)
    for status in ("flagged", "success"):
        PaymentAttempt.objects.filter(pk=a.pk).update(status=status)
        with mock.patch.object(FakeGateway, "verify") as verify:
            r = client_a.post(reverse("fees:attempt_recheck", args=[a.pk]))
        assert r.status_code == 302
        verify.assert_not_called()
        assert _status(a) == status


def test_recheck_unknown_attempt_is_404(w, client_a):
    login_verified(client_a, w.bursar)
    assert client_a.post(reverse("fees:attempt_recheck", args=[10**9])).status_code == 404


def test_abandon_loses_to_a_success_that_lands_during_verify(w):
    """The gateway call runs unlocked; a confirmation landing meanwhile wins."""
    a = _attempt(w, 25 * 60)

    def verify(reference):
        PaymentAttempt.objects.filter(pk=a.pk).update(status="success")
        return mock.Mock(status="failed", raw={})

    with mock.patch.object(FakeGateway, "verify", side_effect=verify):
        tasks.reverify_school_task.apply(headers={"schema_name": "school_a"})
    assert _status(a) == PaymentAttempt.SUCCESS
