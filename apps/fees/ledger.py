"""The ledger: the only place a Payment, its Allocations and its Receipt are created.

Lock order inside ``confirm_payment``: invoice rows ``FOR UPDATE`` in ascending pk order, then
the ``ReceiptCounter`` row for the year. No school lock is taken here, so callers may hold
``school_lock(f"fees:student:{pk}")`` first (the same order as ``invoicing``).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.academics.services import academic_settings_or_defaults
from apps.audit.services import record
from apps.fees import hooks, invoicing
from apps.fees.models import (
    Allocation,
    Invoice,
    ManualPayment,
    Payment,
    PaymentAttempt,
    Receipt,
    ReceiptCounter,
)
from apps.students.models import Student

FLAG_OVERPAID = "Paid more than the balance"
METHODS = frozenset(code for code, _ in Payment.METHOD_CHOICES)


@dataclass(frozen=True)
class PlannedLine:
    invoice_id: int
    amount_kobo: int


def next_receipt_number(year: int) -> str:
    """Take the next receipt number for ``year``. Call only inside ``confirm_payment``'s
    transaction, after the invoice locks: the counter row stays locked until commit, and a
    rollback gives the number back."""
    ReceiptCounter.objects.get_or_create(year=year)
    counter = ReceiptCounter.objects.select_for_update().get(year=year)
    counter.last += 1
    counter.save(update_fields=["last"])
    prefix = academic_settings_or_defaults().admission_prefix
    return f"{prefix}/RCT/{year}/{counter.last:06d}"


def _existing_payment(
    gateway_reference: str | None,
    manual: ManualPayment | None = None,
    attempt: PaymentAttempt | None = None,
) -> Payment | None:
    if gateway_reference:
        found = Payment.objects.filter(gateway_reference=gateway_reference).first()
        if found is not None:
            return found
    if manual is not None:
        found = Payment.objects.filter(manual=manual).first()
        if found is not None:
            return found
    if attempt is not None:
        return Payment.objects.filter(attempt=attempt).first()
    return None


def _validate(amount_kobo: int, method: str, plan: list[PlannedLine], paid_at: datetime) -> None:
    if not isinstance(amount_kobo, int) or amount_kobo <= 0:
        raise ValueError("A payment amount must be a positive number of kobo.")
    if method not in METHODS:
        raise ValueError(f"Unknown payment method {method!r}.")
    if not isinstance(paid_at, datetime) or timezone.is_naive(paid_at):
        raise ValueError("paid_at must be a timezone-aware datetime.")
    for line in plan:
        if line.amount_kobo < 0:
            raise ValueError("A planned amount cannot be negative.")


def _available(invoice: Invoice) -> int:
    """What the invoice can still take. A carried invoice takes nothing."""
    if invoice.status == Invoice.CARRIED:
        return 0
    return max(invoice.balance_kobo, 0)


def confirm_payment(
    *,
    student: Student,
    amount_kobo: int,
    method: str,
    plan: list[PlannedLine],
    paid_at: datetime,
    gateway_reference: str | None = None,
    recorded_by: Any = None,
    approved_by: Any = None,
    manual: ManualPayment | None = None,
    attempt: PaymentAttempt | None = None,
) -> Payment:
    """Record a payment, allocate it across ``plan`` in order and number its receipt.

    Idempotent on ``gateway_reference``, ``manual`` and ``attempt``: a second call with the
    same reference, or for the same ManualPayment or PaymentAttempt, returns the first Payment
    unchanged. Each planned line is capped at the invoice's balance and at what is left of the
    payment; any excess is kept as ``unallocated_kobo`` and the payment flagged.
    """
    _validate(amount_kobo, method, plan, paid_at)
    gateway_reference = gateway_reference or None

    with transaction.atomic():
        ids = sorted({line.invoice_id for line in plan})
        invoices = {
            inv.pk: inv
            for inv in Invoice.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        }
        existing = _existing_payment(gateway_reference, manual, attempt)
        if existing is not None:
            return existing
        if len(invoices) != len(ids):
            raise ValueError("The plan names an invoice that does not exist.")

        remaining = amount_kobo
        planned: dict[int, int] = {}  # invoice pk -> kobo, in plan order
        for line in plan:
            invoice = invoices[line.invoice_id]
            take = min(
                line.amount_kobo, _available(invoice) - planned.get(invoice.pk, 0), remaining
            )
            if take <= 0:
                continue
            planned[invoice.pk] = planned.get(invoice.pk, 0) + take
            remaining -= take

        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    student=student,
                    amount_kobo=amount_kobo,
                    method=method,
                    gateway_reference=gateway_reference,
                    paid_at=paid_at,
                    recorded_by=recorded_by,
                    approved_by=approved_by,
                    unallocated_kobo=remaining,
                    flagged=remaining > 0,
                    flag_reason=FLAG_OVERPAID if remaining > 0 else "",
                    manual=manual,
                    attempt=attempt,
                )
        except IntegrityError:
            # Lost a race on the unique reference or a one-to-one link: re-read.
            if gateway_reference is not None:
                return Payment.objects.get(gateway_reference=gateway_reference)
            if manual is not None:
                return Payment.objects.get(manual=manual)
            if attempt is not None:
                return Payment.objects.get(attempt=attempt)
            raise

        Allocation.objects.bulk_create(
            Allocation(payment=payment, invoice_id=pk, amount_kobo=kobo)
            for pk, kobo in planned.items()
        )
        for pk, kobo in planned.items():
            invoice = invoices[pk]
            invoice.paid_kobo += kobo
            invoice.save(update_fields=["paid_kobo", "updated_at"])
            invoicing.recompute(invoice)

        year = timezone.localtime(timezone.now()).year
        receipt = Receipt.objects.create(
            payment=payment, number=next_receipt_number(year), year=year
        )

        actor = approved_by or recorded_by
        record(
            "fees.payment_confirmed",
            actor=actor,
            obj=payment,
            after={
                "payment_id": payment.pk,
                "student_id": student.pk,
                "method": method,
                "amount_kobo": amount_kobo,
                "unallocated_kobo": remaining,
                "allocations": [
                    {"invoice_id": pk, "amount_kobo": kobo} for pk, kobo in planned.items()
                ],
                "receipt_id": receipt.pk,
            },
        )
        if payment.flagged:
            record(
                "fees.payment_flagged",
                actor=actor,
                obj=payment,
                after={
                    "payment_id": payment.pk,
                    "amount_kobo": amount_kobo,
                    "unallocated_kobo": remaining,
                },
            )

        payment_id = payment.pk
        transaction.on_commit(lambda: hooks.on_payment_confirmed(payment_id))
    return payment
