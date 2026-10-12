import re
from datetime import UTC, datetime
from unittest import mock

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSettings
from apps.audit.models import AuditEvent
from apps.fees import hooks, invoicing, ledger
from apps.fees.ledger import PlannedLine
from apps.fees.models import (
    Allocation,
    Invoice,
    InvoiceLine,
    Payment,
    Receipt,
    ReceiptCounter,
)
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff, make_terms
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

PAID_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _world():
    w = make_terms()
    AcademicSettings.objects.update_or_create(pk=1, defaults={"admission_prefix": "GFS"})
    w.student = StudentProfileFactory()
    w.bursar = make_staff("bursar")
    return w


def _invoice(student, term, total_kobo, paid_kobo=0, status=None):
    inv = Invoice.objects.create(student=student, term=term)
    InvoiceLine.objects.create(
        invoice=inv, kind=InvoiceLine.FEE, description="Tuition", amount_kobo=total_kobo
    )
    invoicing.recompute(inv)
    inv.paid_kobo = paid_kobo
    inv.save(update_fields=["paid_kobo"])
    invoicing.recompute(inv)
    if status is not None:
        inv.status = status
        inv.save(update_fields=["status"])
    return inv


def _confirm(w, plan, amount, **kw):
    kw.setdefault("method", "cash")
    kw.setdefault("paid_at", PAID_AT)
    kw.setdefault("recorded_by", w.bursar)
    kw.setdefault("approved_by", w.bursar)
    return ledger.confirm_payment(
        student=w.student,
        amount_kobo=amount,
        plan=[PlannedLine(invoice_id=inv.pk, amount_kobo=a) for inv, a in plan],
        **kw,
    )


def _at(dt):
    return mock.patch("django.utils.timezone.now", return_value=dt)


NOW_2026 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def test_two_invoices_paid_in_full_get_one_receipt(school_a, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        b = _invoice(w.student, w.term2, 2_000_000)
        with mock.patch.object(hooks, "on_payment_confirmed") as hook:
            with _at(NOW_2026), django_capture_on_commit_callbacks(execute=True) as callbacks:
                payment = _confirm(w, [(a, 3_000_000), (b, 2_000_000)], 5_000_000)

        assert payment.amount_kobo == 5_000_000
        assert payment.unallocated_kobo == 0 and payment.flagged is False
        assert payment.recorded_by == w.bursar and payment.approved_by == w.bursar
        allocs = {al.invoice_id: al.amount_kobo for al in payment.allocations.all()}
        assert allocs == {a.pk: 3_000_000, b.pk: 2_000_000}
        for inv, paid in ((a, 3_000_000), (b, 2_000_000)):
            inv.refresh_from_db()
            assert inv.paid_kobo == paid and inv.status == Invoice.PAID
        receipt = Receipt.objects.get(payment=payment)
        assert receipt.number == "GFS/RCT/2026/000001"
        assert receipt.year == 2026 and receipt.pdf_name == ""
        assert len(callbacks) == 1
        hook.assert_called_once_with(payment.pk)

        event = AuditEvent.objects.get(action="fees.payment_confirmed")
        assert event.after["payment_id"] == payment.pk
        assert event.after["amount_kobo"] == 5_000_000
        assert event.after["receipt_id"] == receipt.pk
        assert not AuditEvent.objects.filter(action="fees.payment_flagged").exists()


def test_receipt_numbers_increase_and_restart_each_lagos_year(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 10_000_000)
        with _at(NOW_2026):
            first = _confirm(w, [(a, 100_000)], 100_000)
            second = _confirm(w, [(a, 100_000)], 100_000)
        # 23:30 UTC on 31 Dec 2026 is already 1 Jan 2027 in Lagos; paid_at's year is ignored.
        with _at(datetime(2026, 12, 31, 23, 30, tzinfo=UTC)):
            third = _confirm(w, [(a, 100_000)], 100_000)
        assert first.receipt.number == "GFS/RCT/2026/000001"
        assert second.receipt.number == "GFS/RCT/2026/000002"
        assert third.receipt.number == "GFS/RCT/2027/000001"
        assert third.receipt.year == 2027
        assert dict(ReceiptCounter.objects.values_list("year", "last")) == {2026: 2, 2027: 1}
        a.refresh_from_db()
        assert a.paid_kobo == 300_000 and a.status == Invoice.PART_PAID


def test_overpayment_is_capped_and_flagged(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000, paid_kobo=2_000_000)
        payment = _confirm(w, [(a, 3_000_000)], 3_000_000)
        assert payment.allocations.get().amount_kobo == 1_000_000
        assert payment.unallocated_kobo == 2_000_000
        assert payment.flagged is True
        assert payment.flag_reason == "Paid more than the balance"
        a.refresh_from_db()
        assert a.paid_kobo == 3_000_000 and a.status == Invoice.PAID
        flagged = AuditEvent.objects.get(action="fees.payment_flagged")
        assert flagged.after["payment_id"] == payment.pk
        assert flagged.after["unallocated_kobo"] == 2_000_000
        assert AuditEvent.objects.filter(action="fees.payment_confirmed").count() == 1


def test_same_gateway_reference_returns_same_payment(school_a, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        with django_capture_on_commit_callbacks() as callbacks:
            p1 = _confirm(w, [(a, 1_000_000)], 1_000_000, method="online", gateway_reference="R1")
            p2 = _confirm(w, [(a, 1_000_000)], 1_000_000, method="online", gateway_reference="R1")
        assert p1.pk == p2.pk
        assert Payment.objects.count() == 1
        assert Allocation.objects.count() == 1 and Receipt.objects.count() == 1
        assert len(callbacks) == 1
        assert AuditEvent.objects.filter(action="fees.payment_confirmed").count() == 1
        a.refresh_from_db()
        assert a.paid_kobo == 1_000_000


def test_integrity_error_on_reference_returns_existing(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        p1 = _confirm(w, [(a, 1_000_000)], 1_000_000, method="online", gateway_reference="R2")
        # Simulate a racer that inserted between our check and our insert.
        with mock.patch.object(ledger, "_existing_payment", return_value=None):
            p2 = _confirm(w, [(a, 1_000_000)], 1_000_000, method="online", gateway_reference="R2")
        assert p2.pk == p1.pk
        assert Allocation.objects.count() == 1 and Receipt.objects.count() == 1
        a.refresh_from_db()
        assert a.paid_kobo == 1_000_000
        assert ReceiptCounter.objects.get(year=timezone.localtime().year).last == 1


def test_manual_payments_without_reference_are_not_deduplicated(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        _confirm(w, [(a, 1_000_000)], 1_000_000)
        _confirm(w, [(a, 1_000_000)], 1_000_000)
        assert Payment.objects.count() == 2


def test_rollback_leaves_counter_unchanged(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        with _at(NOW_2026):
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    _confirm(w, [(a, 1_000_000)], 1_000_000)
                    raise RuntimeError("boom")
            assert not Payment.objects.exists()
            assert not ReceiptCounter.objects.filter(year=2026, last__gt=0).exists()
            payment = _confirm(w, [(a, 1_000_000)], 1_000_000)
        assert payment.receipt.number == "GFS/RCT/2026/000001"


def test_carried_invoice_takes_nothing_and_excess_is_flagged(school_a):
    with schema_context("school_a"):
        w = _world()
        carried = _invoice(w.student, w.term1, 3_000_000, status=Invoice.CARRIED)
        open_ = _invoice(w.student, w.term2, 2_000_000)
        payment = _confirm(w, [(carried, 3_000_000), (open_, 500_000)], 3_500_000)
        allocs = {al.invoice_id: al.amount_kobo for al in payment.allocations.all()}
        assert allocs == {open_.pk: 500_000}
        assert payment.unallocated_kobo == 3_000_000 and payment.flagged
        carried.refresh_from_db()
        assert carried.paid_kobo == 0 and carried.status == Invoice.CARRIED
        open_.refresh_from_db()
        assert open_.status == Invoice.PART_PAID


def test_invoices_locked_for_update_in_pk_order_before_counter(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        b = _invoice(w.student, w.term2, 2_000_000)
        assert a.pk < b.pk
        with CaptureQueriesContext(connection) as ctx:
            _confirm(w, [(b, 2_000_000), (a, 3_000_000)], 5_000_000)  # plan order: b, a
        locks = [
            (i, q["sql"]) for i, q in enumerate(ctx.captured_queries) if "FOR UPDATE" in q["sql"]
        ]
        invoice_locks = [(i, sql) for i, sql in locks if 'FROM "fees_invoice"' in sql]
        counter_locks = [(i, sql) for i, sql in locks if 'FROM "fees_receiptcounter"' in sql]
        assert len(invoice_locks) == 1, locks
        sql = invoice_locks[0][1]
        assert re.search(r'ORDER BY "fees_invoice"\."id" ASC', sql), sql
        assert counter_locks and invoice_locks[0][0] < counter_locks[0][0]
        # Plan order still decides allocation order.
        payment = Payment.objects.get()
        assert [al.invoice_id for al in payment.allocations.order_by("pk")] == [b.pk, a.pk]


def test_rejects_bad_input(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        with pytest.raises(ValueError):
            _confirm(w, [(a, 100)], 0)
        with pytest.raises(ValueError):
            _confirm(w, [(a, 100)], 100, method="cheque")
        with pytest.raises(ValueError):
            _confirm(w, [(a, 100)], 100, paid_at=datetime(2026, 10, 1, 9, 0))  # naive
        with pytest.raises(ValueError):
            ledger.confirm_payment(
                student=w.student,
                amount_kobo=100,
                method="cash",
                plan=[PlannedLine(invoice_id=999_999, amount_kobo=100)],
                paid_at=PAID_AT,
            )
        assert not Payment.objects.exists()


def test_allocation_amount_must_be_positive(school_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        payment = _confirm(w, [(a, 100)], 100)
        with pytest.raises(IntegrityError), transaction.atomic():
            Allocation.objects.create(payment=payment, invoice=a, amount_kobo=0)


def test_invoice_page_lists_allocations(client_a):
    with schema_context("school_a"):
        w = _world()
        a = _invoice(w.student, w.term1, 3_000_000)
        with _at(NOW_2026):
            _confirm(w, [(a, 1_000_000)], 1_000_000, method="teller")
    login_verified(client_a, w.bursar)
    body = client_a.get(reverse("fees:invoice", args=[a.pk])).content.decode()
    assert "GFS/RCT/2026/000001" in body
    assert "₦10,000.00" in body
    assert "Teller" in body
    assert "No payments recorded yet." not in body
