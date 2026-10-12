"""Invoice generation, balance brought forward and adjustments.

Lock order (everywhere): ``school_lock(f"fees:student:{student.pk}")`` first, then invoice rows
``FOR UPDATE``. ``add_line`` only takes the invoice row lock and never a school lock.
"""

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q, QuerySet, Sum

from apps.academics.models import Term
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.fees.models import (
    Discount,
    FeeItem,
    FeeStructure,
    Invoice,
    InvoiceLine,
    StudentDiscount,
)
from apps.students.models import Enrolment, Student

BELOW_PAID_MESSAGE = "This change would bring the invoice total below the amount already paid."


class InvoiceCarried(Exception):
    """The invoice's balance was carried into a later invoice; it can no longer change."""


@dataclass(frozen=True)
class GenerationResult:
    created: int
    skipped_existing: int
    skipped_no_structure: int


def _earlier_terms_q(term: Term, *, prefix: str = "term__", inclusive: bool = False) -> Q:
    """Terms before ``term`` by (session start date, term number), as in results' next-term."""
    start = term.session.start_date
    number_cmp = "lte" if inclusive else "lt"
    return Q(**{f"{prefix}session__start_date__lt": start}) | Q(
        **{f"{prefix}session_id": term.session_id, f"{prefix}number__{number_cmp}": term.number}
    )


def _status_for(total: int, paid: int) -> str:
    if total - paid <= 0:
        return Invoice.PAID
    if paid == 0:
        return Invoice.UNPAID
    return Invoice.PART_PAID


def recompute(invoice: Invoice) -> None:
    """Set total and status from lines and paid. Callers must hold the invoice row lock.

    A ``carried`` invoice keeps its status.
    """
    total = invoice.lines.aggregate(t=Sum("amount_kobo"))["t"] or 0
    invoice.total_kobo = total
    if invoice.status != Invoice.CARRIED:
        invoice.status = _status_for(total, invoice.paid_kobo)
    invoice.save(update_fields=["total_kobo", "status", "updated_at"])


def _percent_of(amount: int, percent: int) -> int:
    """``percent``% of ``amount`` kobo, rounded half up, in integers only."""
    return (amount * percent + 50) // 100


def _discount_amount(discount: Discount, fee_total: int) -> int:
    if discount.kind == Discount.PERCENT:
        return _percent_of(fee_total, discount.percent or 0)
    return discount.amount_kobo or 0


def _discount_label(discount: Discount) -> str:
    if discount.kind == Discount.PERCENT:
        return f"Discount: {discount.name} ({discount.percent}%)"
    return f"Discount: {discount.name}"


def _build_invoice(student: Student, term: Term, structure: FeeStructure) -> Invoice:
    """Create the invoice and its lines. Caller holds the student's school lock."""
    invoice = Invoice.objects.create(student=student, term=term)
    lines: list[InvoiceLine] = []
    fee_total = 0
    for line in structure.lines.all():
        if line.is_optional:
            continue
        fee_total += line.amount_kobo
        lines.append(
            InvoiceLine(
                invoice=invoice,
                kind=InvoiceLine.FEE,
                description=line.item.name,
                item_id=line.item_id,
                amount_kobo=line.amount_kobo,
            )
        )

    running = fee_total
    held = (
        StudentDiscount.objects.filter(student=student, session_id=term.session_id)
        .select_related("discount")
        .order_by("pk")
    )
    for row in held:
        amount = min(_discount_amount(row.discount, fee_total), running)
        if amount <= 0:
            continue
        running -= amount
        lines.append(
            InvoiceLine(
                invoice=invoice,
                kind=InvoiceLine.DISCOUNT,
                description=_discount_label(row.discount),
                amount_kobo=-amount,
            )
        )

    earlier = (
        Invoice.objects.select_for_update(of=("self",))
        .select_related("term__session")
        .filter(_earlier_terms_q(term), student=student)
        .exclude(status=Invoice.CARRIED)
        .order_by("term__session__start_date", "term__number", "pk")
    )
    for old in earlier:
        if old.balance_kobo <= 0:
            continue
        lines.append(
            InvoiceLine(
                invoice=invoice,
                kind=InvoiceLine.BROUGHT_FORWARD,
                description=(
                    f"Balance brought forward from {old.term.get_number_display()} "
                    f"{old.term.session.name}"
                ),
                amount_kobo=old.balance_kobo,
            )
        )
        old.status = Invoice.CARRIED
        old.save(update_fields=["status", "updated_at"])

    InvoiceLine.objects.bulk_create(lines)
    recompute(invoice)
    return invoice


def generate_invoices(
    term: Term, *, levels: Any = None, by: Any, request: Any = None
) -> GenerationResult:
    """Invoice every actively enrolled student in ``term.session`` whose level has a structure.

    Existing invoices for (student, term) are never touched, so this is safe to run again.
    """
    term = Term.objects.select_related("session").get(pk=term.pk)
    level_ids = None if levels is None else sorted({getattr(lv, "pk", lv) for lv in levels})
    enrolments = (
        Enrolment.objects.filter(
            session_id=term.session_id,
            status=Enrolment.Status.ACTIVE,
            student__status=Student.Status.ACTIVE,
        )
        .select_related("student", "arm")
        .order_by("student_id")
    )
    if level_ids is not None:
        enrolments = enrolments.filter(arm__level_id__in=level_ids)
    structures = {
        s.level_id: s
        for s in FeeStructure.objects.filter(term=term).prefetch_related("lines__item")
    }

    created = skipped_existing = skipped_no_structure = 0
    for enrolment in enrolments:
        structure = structures.get(enrolment.arm.level_id)
        if structure is None:
            skipped_no_structure += 1
            continue
        student = enrolment.student
        with transaction.atomic():
            school_lock(f"fees:student:{student.pk}")
            if Invoice.objects.filter(student=student, term=term).exists():
                skipped_existing += 1
                continue
            _build_invoice(student, term, structure)
            created += 1

    result = GenerationResult(created, skipped_existing, skipped_no_structure)
    record(
        "fees.invoices_generated",
        request=request,
        actor=by,
        obj=term,
        after={
            "term_id": term.pk,
            "level_ids": level_ids,
            "created": created,
            "skipped_existing": skipped_existing,
            "skipped_no_structure": skipped_no_structure,
        },
    )
    return result


ADDABLE_KINDS = (InvoiceLine.OPTIONAL, InvoiceLine.ADJUSTMENT)


def add_line(
    invoice: Invoice,
    *,
    kind: str,
    description: str,
    amount_kobo: int,
    reason: str,
    item: FeeItem | None = None,
    by: Any,
    request: Any = None,
) -> InvoiceLine:
    """Add an optional item or an adjustment and recompute the total under the row lock.

    Raises ``InvoiceCarried`` for carried invoices and ``ValidationError`` when the new total
    would fall below the amount already paid.
    """
    if kind not in ADDABLE_KINDS:
        raise ValueError(f"Cannot add a {kind!r} line by hand.")
    reason = (reason or "").strip()
    if kind == InvoiceLine.ADJUSTMENT and not reason:
        raise ValidationError("Give a reason for the adjustment.")
    if kind == InvoiceLine.OPTIONAL and amount_kobo < 0:
        raise ValidationError("An optional item cannot have a negative amount.")
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.status == Invoice.CARRIED:
            raise InvoiceCarried
        before = {"total_kobo": locked.total_kobo, "paid_kobo": locked.paid_kobo}
        if locked.total_kobo + amount_kobo < locked.paid_kobo:
            raise ValidationError(BELOW_PAID_MESSAGE)
        line = InvoiceLine.objects.create(
            invoice=locked,
            kind=kind,
            description=description,
            item=item,
            amount_kobo=amount_kobo,
            reason=reason,
            created_by=by,
        )
        recompute(locked)
        record(
            "fees.invoice_adjusted",
            request=request,
            actor=by,
            obj=locked,
            before=before,
            after={
                "invoice_id": locked.pk,
                "line_id": line.pk,
                "kind": kind,
                "amount_kobo": amount_kobo,
                "total_kobo": locked.total_kobo,
                "paid_kobo": locked.paid_kobo,
            },
        )
    for field in ("total_kobo", "paid_kobo", "status", "updated_at"):
        setattr(invoice, field, getattr(locked, field))
    return line


def _open_invoices(student: Student, up_to_term: Term | None) -> QuerySet[Invoice]:
    qs = Invoice.objects.filter(student=student).exclude(status=Invoice.CARRIED)
    if up_to_term is not None:
        qs = qs.filter(_earlier_terms_q(up_to_term, inclusive=True))
    return qs


def outstanding_for(student: Student, *, up_to_term: Term | None = None) -> int:
    """Sum of balances of the student's non-carried invoices (up to and including a term)."""
    balance = Sum(F("total_kobo") - F("paid_kobo"))
    total = _open_invoices(student, up_to_term).aggregate(b=balance)["b"]
    return int(total or 0)
