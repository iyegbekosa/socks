"""Server-side verification and confirmation of an online payment (§4.5 steps 5–6, FR-12).

``process_attempt`` runs inside the school's schema (``process_attempt_task``, or the
re-verification job). A webhook only says "look at this reference"; credit is given only when
the gateway's own ``verify`` reports success with the attempt's exact amount, ``NGN`` and this
school's subaccount.

Lock order: the attempt row ``FOR UPDATE`` first, then (inside ``ledger.confirm_payment``)
invoices in pk order, then the ``ReceiptCounter`` row. Manual approval locks its
``ManualPayment`` row and then invoices in pk order, so the two only meet on invoices, which
both take in the same order: no lock cycle. The gateway is called with no transaction open.
"""

import logging
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import record
from apps.fees.gateways import VerifyResult, get_gateway
from apps.fees.ledger import PlannedLine, confirm_payment
from apps.fees.models import PaymentAttempt
from apps.tenancy.models import School

logger = logging.getLogger(__name__)

CURRENCY = "NGN"
# A pending attempt older than this that the gateway does not report as paid is abandoned.
ABANDON_AFTER = timedelta(hours=24)
MISSING = "missing"
# Settled for good: never verified again.
FINAL = frozenset({PaymentAttempt.SUCCESS, PaymentAttempt.FLAGGED})
FINAL_FROM_GATEWAY = {
    "failed": PaymentAttempt.FAILED,
    "abandoned": PaymentAttempt.ABANDONED,
}
MISMATCH_REASONS = {
    "amount": "Amount mismatch",
    "currency": "Currency mismatch",
    "subaccount": "Subaccount mismatch",
}


def _school_subaccount() -> str:
    """This school's subaccount code, read fresh (never from a cached tenant object)."""
    code = (
        School.objects.filter(schema_name=getattr(connection, "schema_name", ""))
        .values_list("paystack_subaccount_code", flat=True)
        .first()
    )
    return code or ""


def _mismatch(attempt: PaymentAttempt, result: VerifyResult) -> str | None:
    """The first field that disagrees with what this attempt expects, or None."""
    if result.amount_kobo != attempt.amount_kobo:
        return "amount"
    if result.currency != CURRENCY:
        return "currency"
    expected = _school_subaccount()
    if not expected or result.subaccount != expected:
        return "subaccount"
    return None


def _finish(attempt: PaymentAttempt, status: str, result: VerifyResult, reason: str = "") -> None:
    attempt.status = status
    attempt.raw_response = result.raw  # already without customer/authorization
    fields = ["status", "raw_response", "updated_at"]
    if reason:
        attempt.flag_reason = reason
        fields.append("flag_reason")
    attempt.save(update_fields=fields)


def _is_stale(attempt: PaymentAttempt) -> bool:
    return attempt.created_at < timezone.now() - ABANDON_AFTER


def process_attempt(reference: str, *, abandon_stale: bool = False) -> str:
    """Verify ``reference`` with the gateway and settle its attempt. Returns the status.

    Only ``success`` and ``flagged`` are final here: they return without calling the gateway.
    A ``failed`` or ``abandoned`` attempt is verified again, because Paystack lets a customer
    retry a charge on the same reference; a matching success is then credited late (audit
    ``fees.attempt_late_success``) instead of the money arriving with no record. Any other
    verify status leaves a failed/abandoned attempt unchanged.

    Idempotent: the attempt is re-checked under its row lock before any write, and
    ``confirm_payment`` is itself idempotent per attempt and gateway reference. With
    ``abandon_stale`` (the re-verification sweep), an attempt still ``pending`` after 24 hours
    whose verify is not a success becomes ``abandoned``, decided under the row lock; a
    ``failed`` attempt is never changed to ``abandoned``. Raises
    ``GatewayUnavailable`` (writing nothing) for the task to retry.
    """
    attempt = PaymentAttempt.objects.filter(reference=reference).first()
    if attempt is None:
        logger.warning("No payment attempt for a routed reference.")
        return MISSING
    if attempt.status in FINAL:
        return attempt.status

    # No lock and no transaction across the network call.
    result = get_gateway().verify(reference)
    in_progress = result.status != "success" and result.status not in FINAL_FROM_GATEWAY
    if in_progress and not (abandon_stale and _is_stale(attempt)):
        return attempt.status  # still in progress at the gateway: leave it

    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
        previous = attempt.status
        if previous in FINAL:
            return previous

        if result.status != "success":
            # Gateway says failed/abandoned: settle a pending attempt; never rewrite one
            # that is already failed or abandoned.
            if previous == PaymentAttempt.PENDING:
                if abandon_stale and _is_stale(attempt):
                    new = PaymentAttempt.ABANDONED
                elif in_progress:
                    return previous  # only reachable for a stale sweep that lost the race
                else:
                    new = FINAL_FROM_GATEWAY[result.status]
                _finish(attempt, new, result)
            return attempt.status

        field = _mismatch(attempt, result)
        if field is not None:
            _finish(attempt, PaymentAttempt.FLAGGED, result, MISMATCH_REASONS[field])
            record(
                "fees.attempt_mismatch",
                obj=attempt,
                after={"attempt_id": attempt.pk, "field": field},
            )
            logger.warning("Payment attempt %s flagged: %s mismatch.", attempt.pk, field)
            return attempt.status

        lines = list(attempt.lines.all())
        confirm_payment(
            student=attempt.student,
            amount_kobo=attempt.amount_kobo,
            method="online",
            plan=[PlannedLine(line.invoice_id, line.amount_kobo) for line in lines],
            paid_at=result.paid_at or timezone.now(),
            gateway_reference=reference,
            attempt=attempt,
        )
        _finish(attempt, PaymentAttempt.SUCCESS, result)
        if previous != PaymentAttempt.PENDING:
            record("fees.attempt_late_success", obj=attempt, after={"attempt_id": attempt.pk})
            logger.warning("Payment attempt %s succeeded after being %s.", attempt.pk, previous)
        return attempt.status
