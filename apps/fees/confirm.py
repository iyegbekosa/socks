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

from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import record
from apps.fees.gateways import VerifyResult, get_gateway
from apps.fees.ledger import PlannedLine, confirm_payment
from apps.fees.models import PaymentAttempt
from apps.tenancy.models import School

logger = logging.getLogger(__name__)

CURRENCY = "NGN"
MISSING = "missing"
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


def process_attempt(reference: str) -> str:
    """Verify ``reference`` with the gateway and settle its attempt. Returns the status.

    Idempotent: a final attempt returns its status without calling the gateway, the attempt
    is re-checked under its row lock before any write, and ``confirm_payment`` is itself
    idempotent per attempt and gateway reference. Raises ``GatewayUnavailable`` (leaving the
    attempt pending) for the task to retry.
    """
    attempt = PaymentAttempt.objects.filter(reference=reference).first()
    if attempt is None:
        logger.warning("No payment attempt for a routed reference.")
        return MISSING
    if attempt.status != PaymentAttempt.PENDING:
        return attempt.status

    # No lock and no transaction across the network call.
    result = get_gateway().verify(reference)

    if result.status == "success":
        field = _mismatch(attempt, result)
    elif result.status in FINAL_FROM_GATEWAY:
        field = None
    else:
        return attempt.status  # still pending at the gateway: leave it

    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
        if attempt.status != PaymentAttempt.PENDING:
            return attempt.status

        if result.status in FINAL_FROM_GATEWAY:
            _finish(attempt, FINAL_FROM_GATEWAY[result.status], result)
            return attempt.status

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
        return attempt.status
