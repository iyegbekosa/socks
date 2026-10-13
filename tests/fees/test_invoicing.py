import re
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, ClassArm, ClassLevel, Term
from apps.audit.models import AuditEvent
from apps.fees import invoicing
from apps.fees.models import FeeStructureLine, Invoice, InvoiceLine, StudentDiscount
from apps.students.models import Enrolment
from tests.fees.factories import (
    make_fixed_discount,
    make_item,
    make_percent_discount,
    make_staff,
    make_structure,
    make_terms,
)
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def _jss1_world():
    """JSS 1 1st term: Tuition 50,000 + PTA 5,000, optional Bus 20,000."""
    w = make_terms()
    w.tuition, w.pta, w.bus = make_item("Tuition"), make_item("PTA"), make_item("Bus")
    w.structure1 = make_structure(
        w.level,
        w.term1,
        [(w.tuition, 5_000_000, False), (w.pta, 500_000, False), (w.bus, 2_000_000, True)],
    )
    w.bursar = make_staff("bursar")
    return w


def _invoice(student, term):
    return Invoice.objects.get(student=student, term=term)


def _kinds(invoice):
    return [line.kind for line in invoice.lines.order_by("pk")]


# --- generation -----------------------------------------------------------


def test_generation_applies_fee_lines_then_discounts(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        for d in (
            make_percent_discount("Staff child", 10),
            make_fixed_discount("Bursary", 200_000),
        ):
            StudentDiscount.objects.create(student=student, discount=d, session=w.session)

        result = invoicing.generate_invoices(w.term1, by=w.bursar)

        assert result == invoicing.GenerationResult(
            created=1, skipped_existing=0, skipped_no_structure=0
        )
        inv = _invoice(student, w.term1)
        assert inv.total_kobo == 5_500_000 - 550_000 - 200_000 == 4_750_000
        assert inv.paid_kobo == 0 and inv.balance_kobo == 4_750_000
        assert inv.status == Invoice.UNPAID
        lines = list(inv.lines.order_by("pk"))
        assert [ln.kind for ln in lines] == ["fee", "fee", "discount", "discount"]
        assert {(ln.item_id, ln.amount_kobo) for ln in lines[:2]} == {
            (w.tuition.pk, 5_000_000),
            (w.pta.pk, 500_000),
        }
        assert [ln.amount_kobo for ln in lines[2:]] == [-550_000, -200_000]
        assert not inv.lines.filter(item=w.bus).exists()  # optional lines are not invoiced

        events = list(AuditEvent.objects.filter(action="fees.invoices_generated"))
        assert len(events) == 1
        assert events[0].after["created"] == 1


def test_percent_discount_rounds_half_up_in_kobo(school_a):
    with schema_context("school_a"):
        w = make_terms()
        make_structure(w.level, w.term1, [(make_item("Tuition"), 1_005, False)])
        student = StudentProfileFactory()
        StudentDiscount.objects.create(
            student=student, discount=make_percent_discount("Half", 50), session=w.session
        )
        invoicing.generate_invoices(w.term1, by=make_staff("bursar"))
        inv = _invoice(student, w.term1)
        # 50% of 1,005 kobo is 502.5 -> 503 (half up)
        assert inv.lines.get(kind="discount").amount_kobo == -503
        assert inv.total_kobo == 502


def test_discounts_are_capped_at_zero_and_invoice_is_paid(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        for d in (make_percent_discount("Full", 100), make_fixed_discount("Extra", 100_000)):
            StudentDiscount.objects.create(student=student, discount=d, session=w.session)
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)
        assert inv.total_kobo == 0
        assert inv.status == Invoice.PAID


def test_discounts_from_another_session_do_not_apply(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        other = AcademicSession.objects.create(
            name="2025/2026", start_date=date(2025, 9, 1), end_date=date(2026, 7, 31)
        )
        student = StudentProfileFactory()
        StudentDiscount.objects.create(
            student=student, discount=make_percent_discount("Full", 100), session=other
        )
        invoicing.generate_invoices(w.term1, by=w.bursar)
        assert _invoice(student, w.term1).total_kobo == 5_500_000


def test_balance_brought_forward_and_carried(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        make_structure(w.level, w.term2, [(w.tuition, 5_000_000, False)])
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        first = _invoice(student, w.term1)
        Invoice.objects.filter(pk=first.pk).update(paid_kobo=4_500_000, status=Invoice.PART_PAID)
        assert invoicing.outstanding_for(student) == 1_000_000

        invoicing.generate_invoices(w.term2, by=w.bursar)

        second = _invoice(student, w.term2)
        bf = second.lines.get(kind="brought_forward")
        assert bf.amount_kobo == 1_000_000
        assert bf.description == "Balance brought forward from 1st Term 2026/2027"
        assert _kinds(second) == ["fee", "brought_forward"]
        assert second.total_kobo == 6_000_000
        first.refresh_from_db()
        assert first.status == Invoice.CARRIED
        assert invoicing.outstanding_for(student) == 6_000_000  # counted once
        assert invoicing.outstanding_for(student, up_to_term=w.term1) == 0
        assert invoicing.outstanding_for(student, up_to_term=w.term2) == 6_000_000


def test_brought_forward_crosses_sessions_and_skips_settled(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        # A fully paid invoice is not carried.
        paid = _invoice(student, w.term1)
        Invoice.objects.filter(pk=paid.pk).update(paid_kobo=paid.total_kobo, status=Invoice.PAID)

        nxt = AcademicSession.objects.create(
            name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
        )
        term_next = Term.objects.create(
            session=nxt, number=1, start_date=date(2027, 9, 1), end_date=date(2027, 12, 15)
        )
        level2 = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
        arm2 = ClassArm.objects.create(level=level2, suffix="A")
        Enrolment.objects.create(student=student, arm=arm2, session=nxt)
        make_structure(level2, term_next, [(w.tuition, 6_000_000, False)])
        invoicing.generate_invoices(term_next, by=w.bursar)
        inv = _invoice(student, term_next)
        assert _kinds(inv) == ["fee"]
        paid.refresh_from_db()
        assert paid.status == Invoice.PAID


def test_generation_skips_students_with_a_later_invoice(school_a):
    """Ruling S: generating an earlier term after a later one skips those students."""
    with schema_context("school_a"):
        w = _jss1_world()
        make_structure(w.level, w.term2, [(w.tuition, 5_000_000, False)])
        ahead = StudentProfileFactory()
        invoicing.generate_invoices(w.term2, by=w.bursar)
        newcomer = StudentProfileFactory()
        result = invoicing.generate_invoices(w.term1, by=w.bursar)
        assert result == invoicing.GenerationResult(
            created=1, skipped_existing=0, skipped_no_structure=0, skipped_later_exists=1
        )
        assert not Invoice.objects.filter(student=ahead, term=w.term1).exists()
        assert _invoice(newcomer, w.term1).total_kobo == 5_500_000
        assert _invoice(ahead, w.term2).status == Invoice.UNPAID
        event = AuditEvent.objects.filter(action="fees.invoices_generated").latest("pk")
        assert event.after["skipped_later_exists"] == 1


def test_generation_locks_earlier_invoices_in_pk_order_and_carries_in_term_order(school_a):
    """M4: same lock order as the ledger, even when pk order differs from term order."""
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        term3 = Term.objects.create(
            session=w.session, number=3, start_date=date(2027, 4, 20), end_date=date(2027, 7, 20)
        )
        make_structure(w.level, term3, [(w.tuition, 1_000_000, False)])
        # A late earlier-term invoice: the 2nd term's has the lower pk.
        for term, amount in ((w.term2, 2_000_000), (w.term1, 3_000_000)):
            inv = Invoice.objects.create(student=student, term=term)
            InvoiceLine.objects.create(
                invoice=inv, kind=InvoiceLine.FEE, description="Tuition", amount_kobo=amount
            )
            invoicing.recompute(inv)
        with CaptureQueriesContext(connection) as ctx:
            invoicing.generate_invoices(term3, by=w.bursar)
        locks = [
            q["sql"]
            for q in ctx.captured_queries
            if "FOR UPDATE" in q["sql"] and 'FROM "fees_invoice"' in q["sql"]
        ]
        assert len(locks) == 1 and re.search(r'ORDER BY "fees_invoice"\."id" ASC', locks[0])
        bf = _invoice(student, term3).lines.filter(kind="brought_forward").order_by("pk")
        assert [line.amount_kobo for line in bf] == [3_000_000, 2_000_000]  # 1st term first


def test_generation_is_idempotent_and_picks_up_new_students(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        first = StudentProfileFactory()
        r1 = invoicing.generate_invoices(w.term1, by=w.bursar)
        assert r1.created == 1
        r2 = invoicing.generate_invoices(w.term1, by=w.bursar)
        assert r2 == invoicing.GenerationResult(
            created=0, skipped_existing=1, skipped_no_structure=0
        )
        assert Invoice.objects.count() == 1
        old = _invoice(first, w.term1)
        old_lines = list(old.lines.values_list("pk", "amount_kobo"))

        FeeStructureLine.objects.filter(structure=w.structure1, item=w.tuition).update(
            amount_kobo=6_000_000
        )
        newcomer = StudentProfileFactory()
        r3 = invoicing.generate_invoices(w.term1, by=w.bursar)
        assert r3 == invoicing.GenerationResult(
            created=1, skipped_existing=1, skipped_no_structure=0
        )
        assert _invoice(newcomer, w.term1).total_kobo == 6_500_000
        old.refresh_from_db()
        assert old.total_kobo == 5_500_000
        assert list(old.lines.values_list("pk", "amount_kobo")) == old_lines
        assert Invoice.objects.count() == 2


def test_level_without_structure_is_skipped_and_levels_filter(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        level2 = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
        arm2 = ClassArm.objects.create(level=level2, suffix="A")
        StudentProfileFactory()
        StudentProfileFactory(enrolment__arm=arm2)
        withdrawn = StudentProfileFactory(enrolment__status="withdrawn")

        only2 = invoicing.generate_invoices(w.term1, levels=[level2], by=w.bursar)
        assert only2 == invoicing.GenerationResult(
            created=0, skipped_existing=0, skipped_no_structure=1
        )
        result = invoicing.generate_invoices(w.term1, by=w.bursar)
        assert result == invoicing.GenerationResult(
            created=1, skipped_existing=0, skipped_no_structure=1
        )
        assert not Invoice.objects.filter(student=withdrawn).exists()


def test_generation_audit_holds_counts_only(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        event = AuditEvent.objects.get(action="fees.invoices_generated")
        assert event.after == {
            "term_id": w.term1.pk,
            "level_ids": None,
            "created": 1,
            "skipped_existing": 0,
            "skipped_no_structure": 0,
            "skipped_later_exists": 0,
        }


# --- adjustments ----------------------------------------------------------


def test_add_adjustment_recomputes_and_audits(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)

        line = invoicing.add_line(
            inv,
            kind=InvoiceLine.ADJUSTMENT,
            description="Adjustment",
            amount_kobo=-300_000,
            reason="Sibling waiver agreed by proprietor",
            by=w.bursar,
        )
        inv.refresh_from_db()
        assert inv.total_kobo == 5_200_000
        assert inv.status == Invoice.UNPAID
        assert line.created_by == w.bursar and line.reason.startswith("Sibling")

        event = AuditEvent.objects.get(action="fees.invoice_adjusted")
        assert event.object_id == str(inv.pk)
        assert event.after == {
            "invoice_id": inv.pk,
            "line_id": line.pk,
            "kind": "adjustment",
            "amount_kobo": -300_000,
            "total_kobo": 5_200_000,
            "paid_kobo": 0,
        }
        assert event.before == {"total_kobo": 5_500_000, "paid_kobo": 0}
        assert "Sibling" not in str(event.after) + str(event.before)


def test_adjustment_requires_reason(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)
        with pytest.raises(ValidationError):
            invoicing.add_line(
                inv,
                kind=InvoiceLine.ADJUSTMENT,
                description="Adjustment",
                amount_kobo=-100,
                reason="  ",
                by=w.bursar,
            )


def test_optional_line_raises_total_and_part_paid_status(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)
        Invoice.objects.filter(pk=inv.pk).update(paid_kobo=5_500_000, status=Invoice.PAID)
        invoicing.add_line(
            inv,
            kind=InvoiceLine.OPTIONAL,
            description="Bus",
            item=w.bus,
            amount_kobo=2_000_000,
            reason="",
            by=w.bursar,
        )
        inv.refresh_from_db()
        assert inv.total_kobo == 7_500_000
        assert inv.status == Invoice.PART_PAID


def test_adjustment_below_paid_is_refused(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)
        Invoice.objects.filter(pk=inv.pk).update(paid_kobo=5_000_000, status=Invoice.PART_PAID)
        with pytest.raises(ValidationError):
            invoicing.add_line(
                inv,
                kind=InvoiceLine.ADJUSTMENT,
                description="Adjustment",
                amount_kobo=-600_000,
                reason="Too much",
                by=w.bursar,
            )
        inv.refresh_from_db()
        assert inv.total_kobo == 5_500_000
        assert inv.lines.count() == 2
        assert not AuditEvent.objects.filter(action="fees.invoice_adjusted").exists()


def test_carried_invoice_refuses_lines_and_keeps_status(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)
        Invoice.objects.filter(pk=inv.pk).update(status=Invoice.CARRIED)
        with pytest.raises(invoicing.InvoiceCarried):
            invoicing.add_line(
                inv,
                kind=InvoiceLine.ADJUSTMENT,
                description="Adjustment",
                amount_kobo=-100,
                reason="x",
                by=w.bursar,
            )
        with transaction.atomic():
            locked = Invoice.objects.select_for_update().get(pk=inv.pk)
            invoicing.recompute(locked)
        locked.refresh_from_db()
        assert locked.status == Invoice.CARRIED


def test_invoice_constraints(school_a):
    with schema_context("school_a"):
        w = _jss1_world()
        student = StudentProfileFactory()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv = _invoice(student, w.term1)
        with pytest.raises(IntegrityError), transaction.atomic():
            Invoice.objects.create(student=student, term=w.term1)
        with pytest.raises(IntegrityError), transaction.atomic():
            Invoice.objects.filter(pk=inv.pk).update(paid_kobo=inv.total_kobo + 1)
        with pytest.raises(IntegrityError), transaction.atomic():
            InvoiceLine.objects.create(
                invoice=inv, kind=InvoiceLine.ADJUSTMENT, description="x", amount_kobo=1
            )
