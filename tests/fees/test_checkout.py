"""Online checkout (§4.5 steps 1–3): attempts, public routing, gateway initialize."""

import logging
import re
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.fees import checkout, hooks, ledger, manual
from apps.fees.gateways import GatewayUnavailable
from apps.fees.gateways.fake import FakeGateway
from apps.fees.ledger import PlannedLine
from apps.fees.models import (
    FeeSettings,
    Invoice,
    PaymentAttempt,
    PaymentAttemptLine,
)
from apps.tenancy.models import PaymentRoute, School
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _invoice, _world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

SUBACCOUNT = "ACCT_school_a_test"
BANK_NOTE = "Pay into GTBank 0123456789, School A Ltd. Write the admission number on the teller."
REFERENCE_RE = re.compile(r"^B2L-[A-Z2-7]{20}$")
CALLBACK = "http://school-a.localhost/fees/pay/return/"


@pytest.fixture(autouse=True)
def _fake_gateway(settings):
    settings.DEBUG = True
    settings.PAYMENT_GATEWAY = "apps.fees.gateways.fake.FakeGateway"
    settings.PAYMENT_PLACEHOLDER_EMAIL_DOMAIN = "build2learn.ng"
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _no_receipt_pdf():
    with mock.patch.object(hooks, "on_payment_confirmed"):
        yield


def make_ready(schema: str = "school_a", code: str = SUBACCOUNT) -> None:
    School.objects.filter(schema_name=schema).update(paystack_subaccount_code=code)


@pytest.fixture
def ready(school_a):
    make_ready()


def _cworld():
    """A paying student with a ₦100,000 invoice and a sibling with a ₦50,000 invoice."""
    w = _world()
    w.inv = _invoice(w.student, w.term1, 10_000_000)
    w.sibling = StudentProfileFactory()
    w.sib_inv = _invoice(w.sibling, w.term1, 5_000_000)
    w.other = StudentProfileFactory()  # not a sibling
    w.other_inv = _invoice(w.other, w.term1, 4_000_000)
    conf = FeeSettings.load()
    conf.bank_transfer_note = BANK_NOTE
    conf.save()
    return w


@pytest.fixture
def w(school_a, ready):
    with schema_context("school_a"):
        world = _cworld()
        with mock.patch("apps.fees.siblings.siblings_of", return_value=[world.sibling]):
            yield world


def _settings(allow=True, minimum=0):
    conf = FeeSettings.load()
    conf.allow_part_payment = allow
    conf.min_part_payment_kobo = minimum
    conf.save()


def _start(w, lines, student=None):
    return checkout.start(
        student or w.student,
        [PlannedLine(inv.pk, amount) for inv, amount in lines],
        callback_url=CALLBACK,
    )


def _naira(kobo: int) -> str:
    return f"{kobo // 100}.{kobo % 100:02d}"


def _pay_post(client, lines):
    data: dict[str, list[str] | str] = {"invoice": [str(inv.pk) for inv, _ in lines]}
    for inv, amount in lines:
        data[f"amount_{inv.pk}"] = _naira(amount)
    return client.post(reverse("fees:pay"), data)


# --- the pay page -------------------------------------------------------------------------


def test_paying_own_and_sibling_invoices_in_full(w, client_a):
    client_a.force_login(w.student.user)
    r = _pay_post(client_a, [(w.inv, 10_000_000), (w.sib_inv, 5_000_000)])
    assert r.status_code == 302, r.content
    with schema_context("school_a"):
        attempt = PaymentAttempt.objects.get()
        assert REFERENCE_RE.match(attempt.reference)
        assert attempt.student == w.student
        assert attempt.amount_kobo == 15_000_000
        assert attempt.status == PaymentAttempt.PENDING
        assert attempt.gateway == "paystack"
        lines = list(attempt.lines.values_list("invoice_id", "amount_kobo"))
        assert lines == [(w.inv.pk, 10_000_000), (w.sib_inv.pk, 5_000_000)]
        state = FakeGateway.lookup(attempt.reference)
        assert state is not None
        assert state["amount_kobo"] == 15_000_000
        assert state["subaccount"] == SUBACCOUNT
        assert state["metadata"] == {"schema": "school_a", "attempt_id": attempt.pk}
        callback = urlsplit(state["callback_url"])
        assert callback.scheme in ("http", "https") and callback.netloc
        assert callback.path == reverse("fees:pay_return")
        assert parse_qs(callback.query) == {"reference": [attempt.reference]}
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 0  # starting a payment credits nothing
    fake_path = reverse("fees:fake_checkout", args=[attempt.reference])
    assert urlsplit(r["Location"]).path == fake_path
    with schema_context("public"):
        route = PaymentRoute.objects.get(reference=attempt.reference)
        assert route.schema_name == "school_a"


def test_audit_holds_ids_and_kobo_only(w):
    w.student.guardian_email = "mum@example.com"
    w.student.save()
    attempt = _start(w, [(w.inv, 10_000_000), (w.sib_inv, 5_000_000)])
    ev = AuditEvent.objects.get(action="fees.attempt_started")
    assert ev.after == {
        "attempt_id": attempt.pk,
        "amount_kobo": 15_000_000,
        "invoice_ids": [w.inv.pk, w.sib_inv.pk],
    }
    assert ev.object_id == str(attempt.pk)
    assert "mum@example.com" not in str(ev.after) + str(ev.before)


def test_pay_page_lists_own_and_sibling_invoices_with_amount_inputs(w, client_a):
    client_a.force_login(w.student.user)
    r = client_a.get(reverse("fees:pay"))
    assert r.status_code == 200
    body = r.content.decode()
    assert f'name="amount_{w.inv.pk}"' in body
    assert f'name="amount_{w.sib_inv.pk}"' in body
    assert f'name="amount_{w.other_inv.pk}"' not in body
    assert "₦100,000.00" in body and "₦50,000.00" in body
    assert w.sibling.first_name in body


def test_pay_page_without_part_payment_has_no_amount_inputs(w, client_a):
    _settings(allow=False)
    client_a.force_login(w.student.user)
    body = client_a.get(reverse("fees:pay")).content.decode()
    assert f'type="hidden" name="amount_{w.inv.pk}"' in body
    assert f'type="text" name="amount_{w.inv.pk}"' not in body


def test_staff_cannot_use_pay_page(w, client_a):
    client_a.force_login(make_staff("teacher"))
    assert client_a.get(reverse("fees:pay")).status_code == 403
    assert client_a.get(reverse("fees:pay_return") + "?reference=B2L-X").status_code == 403


def test_pay_page_refuses_non_sibling_invoice_with_404(w, client_a):
    client_a.force_login(w.student.user)
    r = _pay_post(client_a, [(w.inv, 10_000_000), (w.other_inv, 4_000_000)])
    assert r.status_code == 404
    with schema_context("school_a"):
        assert not PaymentAttempt.objects.exists()


def test_pay_page_bad_invoice_id_is_404(w, client_a):
    client_a.force_login(w.student.user)
    r = client_a.post(reverse("fees:pay"), {"invoice": ["abc"], "amount_abc": "10.00"})
    assert r.status_code == 404


def test_pay_page_shows_validation_errors(w, client_a):
    _settings(allow=True, minimum=500_000)
    client_a.force_login(w.student.user)
    r = _pay_post(client_a, [(w.inv, 400_000)])
    assert r.status_code == 200
    assert checkout.MSG_BELOW_MINIMUM.format(minimum="₦5,000.00") in r.content.decode()
    r = client_a.post(reverse("fees:pay"), {"invoice": [str(w.inv.pk)], f"amount_{w.inv.pk}": "x"})
    assert r.status_code == 200 and checkout.MSG_AMOUNT in r.content.decode()
    r = client_a.post(reverse("fees:pay"), {})
    assert r.status_code == 200 and checkout.MSG_EMPTY in r.content.decode()
    with schema_context("school_a"):
        assert not PaymentAttempt.objects.exists()


def test_gateway_down_shows_bank_transfer_and_fails_attempt(w, client_a):
    client_a.force_login(w.student.user)
    with mock.patch.object(FakeGateway, "initialize", side_effect=GatewayUnavailable("x")):
        r = _pay_post(client_a, [(w.inv, 10_000_000)])
    assert r.status_code == 200
    body = r.content.decode()
    assert (
        "Online payment is not available right now. You can pay by bank transfer and give the "
        "teller to the school bursar."
    ) in body
    assert BANK_NOTE in body
    with schema_context("school_a"):
        attempt = PaymentAttempt.objects.get()
        assert attempt.status == PaymentAttempt.FAILED
        assert attempt.flag_reason == "Gateway unavailable at start"


def test_not_ready_school_shows_message_and_creates_nothing(school_a, client_a):
    make_ready(code="")
    with schema_context("school_a"):
        w = _cworld()
        client_a.force_login(w.student.user)
    msg = "This school has not set up online payments yet."
    r = client_a.get(reverse("fees:pay"))
    assert r.status_code == 200 and msg in r.content.decode()
    r = _pay_post(client_a, [(w.inv, 10_000_000)])
    assert r.status_code == 200 and msg in r.content.decode()
    with schema_context("school_a"):
        assert not PaymentAttempt.objects.exists()
        with pytest.raises(checkout.OnlinePaymentsNotReady):
            checkout.start(w.student, [PlannedLine(w.inv.pk, 10_000_000)], callback_url=CALLBACK)
    with schema_context("public"):
        assert not PaymentRoute.objects.exists()


# --- service rules --------------------------------------------------------------------------


def test_part_payment_minimum(w):
    _settings(allow=True, minimum=500_000)
    with pytest.raises(ValidationError) as exc:
        _start(w, [(w.inv, 400_000)])
    assert checkout.MSG_BELOW_MINIMUM.format(minimum="₦5,000.00") in exc.value.messages
    small = _invoice(w.student, w.term2, 1_000_000, paid_kobo=700_000)  # ₦3,000 left
    attempt = _start(w, [(small, 300_000)])
    assert attempt.amount_kobo == 300_000
    with pytest.raises(ValidationError) as exc:
        _start(w, [(w.inv, 10_000_001)])
    assert checkout.MSG_OVER_BALANCE in exc.value.messages
    attempt = _start(w, [(w.inv, 500_000)])
    assert attempt.amount_kobo == 500_000


def test_part_payment_off_requires_full_balance(w):
    _settings(allow=False)
    for amount in (9_999_999, 1, 10_000_001):
        with pytest.raises(ValidationError) as exc:
            _start(w, [(w.inv, amount)])
        assert checkout.MSG_FULL_BALANCE in exc.value.messages
    assert _start(w, [(w.inv, 10_000_000)]).amount_kobo == 10_000_000
    assert PaymentAttempt.objects.count() == 1


def test_non_sibling_or_unknown_invoice_is_404(w):
    with pytest.raises(Http404):
        _start(w, [(w.other_inv, 4_000_000)])
    with pytest.raises(Http404):
        checkout.start(w.student, [PlannedLine(999_999, 100)], callback_url=CALLBACK)
    assert not PaymentAttempt.objects.exists()


def test_carried_and_settled_invoices_are_refused(w):
    carried = _invoice(w.student, w.term2, 1_000_000, status=Invoice.CARRIED)
    with pytest.raises(ValidationError) as exc:
        _start(w, [(carried, 1_000_000)])
    assert checkout.MSG_CARRIED in exc.value.messages
    carried.delete()
    paid = _invoice(w.student, w.term2, 1_000_000, paid_kobo=1_000_000)
    with pytest.raises(ValidationError) as exc:
        _start(w, [(paid, 1)])
    assert checkout.MSG_SETTLED in exc.value.messages
    assert not PaymentAttempt.objects.exists()


@pytest.mark.parametrize("amounts", [[0], [-5], [100, 0]])
def test_lines_must_be_positive(w, amounts):
    invs = [w.inv, w.sib_inv]
    with pytest.raises(ValidationError):
        _start(w, list(zip(invs, amounts, strict=False)))
    assert not PaymentAttempt.objects.exists()


def test_empty_and_duplicate_lines_are_refused(w):
    with pytest.raises(ValidationError) as exc:
        _start(w, [])
    assert checkout.MSG_EMPTY in exc.value.messages
    with pytest.raises(ValidationError):
        _start(w, [(w.inv, 100), (w.inv, 100)])


def test_reference_collision_is_retried(w):
    first = _start(w, [(w.inv, 100_000)])
    fresh = "B2L-" + "A" * 20
    with mock.patch.object(checkout, "new_reference", side_effect=[first.reference, fresh]):
        second = _start(w, [(w.inv, 100_000)])
    assert second.reference == fresh
    with schema_context("public"):
        assert PaymentRoute.objects.filter(reference=fresh).exists()


def test_reference_collision_gives_up_after_retries(w):
    first = _start(w, [(w.inv, 100_000)])
    with (
        mock.patch.object(checkout, "new_reference", return_value=first.reference) as gen,
        pytest.raises(Exception),  # noqa: B017, PT011 - the IntegrityError surfaces
    ):
        _start(w, [(w.inv, 100_000)])
    assert gen.call_count == 4  # first try + 3 retries
    assert PaymentAttempt.objects.count() == 1


def test_new_reference_format():
    refs = {checkout.new_reference() for _ in range(50)}
    assert len(refs) == 50
    assert all(REFERENCE_RE.match(r) for r in refs)


def test_gateway_is_called_after_commit_outside_transaction(w):
    depth = len(connection.atomic_blocks)  # the test's own transaction
    seen = {}

    def initialize(self, **kw):
        seen["depth"] = len(connection.atomic_blocks)
        seen["attempt_exists"] = PaymentAttempt.objects.filter(reference=kw["reference"]).exists()
        return mock.Mock(authorization_url="https://checkout.example/abc")

    with mock.patch.object(FakeGateway, "initialize", initialize):
        attempt = _start(w, [(w.inv, 100_000)])
    assert seen == {"depth": depth, "attempt_exists": True}
    assert attempt.authorization_url == "https://checkout.example/abc"


def test_gateway_unavailable_marks_failed_and_reraises(w):
    with (
        mock.patch.object(FakeGateway, "initialize", side_effect=GatewayUnavailable("boom")),
        pytest.raises(GatewayUnavailable),
    ):
        _start(w, [(w.inv, 100_000)])
    attempt = PaymentAttempt.objects.get()
    assert attempt.status == PaymentAttempt.FAILED
    assert attempt.flag_reason == "Gateway unavailable at start"
    assert PaymentAttemptLine.objects.filter(attempt=attempt).count() == 1


@pytest.mark.parametrize(
    "g1, g2, expected",
    [
        ("mum@example.com", "dad@example.com", "mum@example.com"),
        ("", "dad@example.com", "dad@example.com"),
        ("", "", None),
    ],
)
def test_customer_email(w, g1, g2, expected, caplog):
    w.student.guardian_email = g1
    w.student.guardian2_email = g2
    w.student.save()
    sent = {}
    real = FakeGateway.initialize

    def spy(self, **kw):
        sent.update(kw)
        return real(self, **kw)

    with caplog.at_level(logging.DEBUG), mock.patch.object(FakeGateway, "initialize", spy):
        attempt = _start(w, [(w.inv, 100_000)])
    want = expected or f"pay+school_a-{w.student.pk}@build2learn.ng"
    assert sent["email"] == want
    assert sent["amount_kobo"] == 100_000 and sent["reference"] == attempt.reference
    assert want not in caplog.text
    assert want not in str(AuditEvent.objects.get(action="fees.attempt_started").after)
    assert want not in str(attempt.raw_response)


# --- the return page and status_for ---------------------------------------------------------


def test_status_for_reads_only(w):
    attempt = _start(w, [(w.inv, 100_000)])
    with mock.patch("apps.fees.checkout.get_gateway", side_effect=AssertionError("no calls")):
        assert checkout.status_for(w.student, attempt.reference) == "pending"
        for status in ("success", "failed", "abandoned", "flagged"):
            PaymentAttempt.objects.filter(pk=attempt.pk).update(status=status)
            assert checkout.status_for(w.student, attempt.reference) == status
        with pytest.raises(Http404):
            checkout.status_for(w.sibling, attempt.reference)
        for bad in ("", "nope", "B2L-" + "Z" * 200):
            with pytest.raises(Http404):
                checkout.status_for(w.student, bad)


def test_pay_return_pending_then_outcomes(w, client_a):
    attempt = _start(w, [(w.inv, 100_000)])
    client_a.force_login(w.student.user)
    url = reverse("fees:pay_return")
    query = f"?reference={attempt.reference}&trxref={attempt.reference}"
    # Paystack appends trxref and reference again; extra params are ignored.
    r = client_a.get(f"{url}{query}&reference={attempt.reference}")
    assert r.status_code == 200
    assert "We are confirming your payment" in r.content.decode()
    with schema_context("school_a"):
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 0 and w.inv.status == Invoice.UNPAID
        assert attempt.__class__.objects.get(pk=attempt.pk).status == "pending"
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status="success")
    assert "Payment received" in client_a.get(url + query).content.decode()
    with schema_context("school_a"):
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status="failed")
    assert "Payment was not completed" in client_a.get(url + query).content.decode()


def test_pay_return_with_someone_elses_reference_is_404(w, client_a):
    attempt = _start(w, [(w.inv, 100_000)])
    client_a.force_login(w.sibling.user)
    url = reverse("fees:pay_return")
    assert client_a.get(f"{url}?reference={attempt.reference}").status_code == 404
    assert client_a.get(url).status_code == 404


# --- ledger link, manual ownership, portal link --------------------------------------------


def test_confirm_payment_links_attempt_and_is_idempotent_on_it(w):
    attempt = _start(w, [(w.inv, 100_000)])
    kw = {
        "student": w.student,
        "amount_kobo": 100_000,
        "method": "online",
        "plan": [PlannedLine(w.inv.pk, 100_000)],
        "paid_at": timezone.now(),
        "attempt": attempt,
    }
    payment = ledger.confirm_payment(**kw)
    assert payment.attempt == attempt and attempt.payment == payment
    again = ledger.confirm_payment(**kw)
    assert again.pk == payment.pk
    w.inv.refresh_from_db()
    assert w.inv.paid_kobo == 100_000


def test_manual_payment_accepts_sibling_invoice(w):
    mp = manual.record(
        student=w.student,
        method="cash",
        amount_kobo=100_000,
        plan=[PlannedLine(w.sib_inv.pk, 100_000)],
        paid_on=timezone.localdate(),
        reference_no="",
        by=w.bursar,
    )
    assert mp.plan == [{"invoice_id": w.sib_inv.pk, "amount_kobo": 100_000}]
    with pytest.raises(ValidationError):
        manual.record(
            student=w.student,
            method="cash",
            amount_kobo=100_000,
            plan=[PlannedLine(w.other_inv.pk, 100_000)],
            paid_on=timezone.localdate(),
            reference_no="",
            by=w.bursar,
        )


def test_siblings_stub_returns_empty_list(school_a):
    from apps.fees.siblings import siblings_of

    with schema_context("school_a"):
        assert siblings_of(StudentProfileFactory()) == []


def test_portal_home_shows_pay_link_only_with_a_balance(w, client_a):
    client_a.force_login(w.student.user)
    pay_url = reverse("fees:pay")
    assert pay_url in client_a.get(reverse("home")).content.decode()
    with schema_context("school_a"):
        Invoice.objects.filter(student=w.student).update(paid_kobo=10_000_000, status="paid")
        Invoice.objects.filter(student=w.sibling).update(paid_kobo=5_000_000, status="paid")
    assert pay_url not in client_a.get(reverse("home")).content.decode()


def test_confirm_payment_race_on_attempt_returns_existing(w):
    attempt = _start(w, [(w.inv, 100_000)])
    kw = {
        "student": w.student,
        "amount_kobo": 100_000,
        "method": "online",
        "plan": [PlannedLine(w.inv.pk, 100_000)],
        "paid_at": timezone.now(),
        "attempt": attempt,
    }
    first = ledger.confirm_payment(**kw)
    with mock.patch.object(ledger, "_existing_payment", return_value=None):
        second = ledger.confirm_payment(**kw)
    assert second.pk == first.pk
    w.inv.refresh_from_db()
    assert w.inv.paid_kobo == 100_000
