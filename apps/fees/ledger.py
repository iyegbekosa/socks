"""The ledger: the only place a Payment, its Allocations and its Receipt are created.

Lock order inside ``confirm_payment``: invoice rows ``FOR UPDATE`` in ascending pk order (the
planned invoices plus, for a planned invoice that was carried, its student's newest non-carried
invoice: Ruling Q), then the ``ReceiptCounter`` row for the year. No school lock is taken here,
so callers may hold ``school_lock(f"fees:student:{pk}")`` first (the same order as
``invoicing``).
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


# How many times to re-resolve redirect targets when a carry races the lock.
_LOCK_TRIES = 3


def _redirect_targets(ids: list[int]) -> dict[int, int]:
    """For each carried invoice in ``ids``: its student's newest non-carried invoice (by the
    shared term ordering). Carried invoices whose student has no open invoice are left out."""
    carried = dict(
        Invoice.objects.filter(pk__in=ids, status=Invoice.CARRIED).values_list("pk", "student_id")
    )
    if not carried:
        return {}
    newest: dict[int, int] = {}
    rows = (
        Invoice.objects.filter(student_id__in=set(carried.values()))
        .exclude(status=Invoice.CARRIED)
        .order_by("student_id", "-term__session__start_date", "-term__number", "-pk")
        .values_list("student_id", "pk")
    )
    for student_id, pk in rows:
        newest.setdefault(student_id, pk)
    return {pk: newest[sid] for pk, sid in carried.items() if sid in newest}


def _lock_invoices(ids: list[int]) -> tuple[dict[int, Invoice], dict[int, int]]:
    """Lock the planned invoices and their redirect targets in one ascending-pk pass.

    Targets are resolved first without a lock, then everything is locked, then re-resolved. If
    a carry committed in between (the target changed), the savepoint is rolled back (releasing
    those row locks) and the pass is repeated. On the last try the locked set is used as is,
    redirecting only to a locked invoice that is still open."""
    for attempt_no in range(_LOCK_TRIES):
        targets = _redirect_targets(ids)
        lock_ids = sorted(set(ids) | set(targets.values()))
        sid = transaction.savepoint()
        invoices = {
            inv.pk: inv
            for inv in Invoice.objects.select_for_update().filter(pk__in=lock_ids).order_by("pk")
        }
        fresh = _redirect_targets(ids)
        if fresh == targets or attempt_no == _LOCK_TRIES - 1:
            transaction.savepoint_commit(sid)
            usable = {
                pk: target
                for pk, target in fresh.items()
                if target in invoices and invoices[target].status != Invoice.CARRIED
            }
            return invoices, usable
        transaction.savepoint_rollback(sid)
    raise AssertionError("unreachable")  # pragma: no cover


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

    A line planned for an invoice that has since been carried (next term generated while the
    payment was pending) goes to that student's newest non-carried invoice, where the debt now
    sits (Ruling Q), still capped at that invoice's balance. Each allocation records the
    invoice's balance just after this payment (``balance_after_kobo``, Ruling T).
    """
    _validate(amount_kobo, method, plan, paid_at)
    gateway_reference = gateway_reference or None

    with transaction.atomic():
        ids = sorted({line.invoice_id for line in plan})
        invoices, targets = _lock_invoices(ids)
        existing = _existing_payment(gateway_reference, manual, attempt)
        if existing is not None:
            return existing
        if any(pk not in invoices for pk in ids):
            raise ValueError("The plan names an invoice that does not exist.")

        remaining = amount_kobo
        planned: dict[int, int] = {}  # invoice pk -> kobo, in plan order
        redirected: list[dict[str, int]] = []
        for line in plan:
            pk = line.invoice_id
            if invoices[pk].status == Invoice.CARRIED and pk in targets:
                pk = targets[pk]
            invoice = invoices[pk]
            take = min(line.amount_kobo, _available(invoice) - planned.get(pk, 0), remaining)
            if take <= 0:
                continue
            planned[pk] = planned.get(pk, 0) + take
            remaining -= take
            if pk != line.invoice_id:
                redirected.append(
                    {"from_invoice_id": line.invoice_id, "to_invoice_id": pk, "amount_kobo": take}
                )

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
            # Lost a race on the unique reference or a one-to-one link: re-read. Anything else
            # (a check constraint, say) finds nothing and the original error is raised.
            links = (
                ("gateway_reference", gateway_reference),
                ("manual", manual),
                ("attempt", attempt),
            )
            for field, value in links:
                if value is not None:
                    found = Payment.objects.filter(**{field: value}).first()
                    if found is not None:
                        return found
            raise

        allocations: list[Allocation] = []
        for pk, kobo in planned.items():
            invoice = invoices[pk]
            invoice.paid_kobo += kobo
            invoice.save(update_fields=["paid_kobo", "updated_at"])
            invoicing.recompute(invoice)
            allocations.append(
                Allocation(
                    payment=payment,
                    invoice_id=pk,
                    amount_kobo=kobo,
                    balance_after_kobo=invoice.balance_kobo,
                )
            )
        Allocation.objects.bulk_create(allocations)

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
                **({"redirected": redirected} if redirected else {}),
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
