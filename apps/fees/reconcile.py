"""Re-verification sweep for one school and the bursar's "mark resolved" action (§4.5 step 7)."""

import logging
from contextlib import nullcontext
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.audit.services import record
from apps.fees.confirm import process_attempt
from apps.fees.gateways import GatewayUnavailable
from apps.fees.models import Payment, PaymentAttempt

logger = logging.getLogger(__name__)

REVERIFY_CAP = 200
PENDING_MIN_AGE = timedelta(minutes=30)
LATE_RETRY_WINDOW = timedelta(hours=24)
MAX_CONSECUTIVE_GATEWAY_ERRORS = 3
NOTE_MAX = 500


class PaymentNotFlaggedError(ValidationError):
    """The payment is no longer flagged (someone else resolved it first)."""


def sweep_references(now=None) -> list[str]:
    """References to re-verify: pending ones older than 30 minutes (oldest first), then failed
    or abandoned ones touched in the last 24 hours (a customer may retry on the same
    reference). At most ``REVERIFY_CAP`` in all."""
    now = now or timezone.now()
    pending = list(
        PaymentAttempt.objects.filter(
            status=PaymentAttempt.PENDING, created_at__lt=now - PENDING_MIN_AGE
        )
        .order_by("created_at", "pk")
        .values_list("reference", flat=True)[:REVERIFY_CAP]
    )
    room = REVERIFY_CAP - len(pending)
    if room <= 0:
        return pending
    late = (
        PaymentAttempt.objects.filter(
            status__in=(PaymentAttempt.FAILED, PaymentAttempt.ABANDONED),
            updated_at__gte=now - LATE_RETRY_WINDOW,
        )
        .order_by("updated_at", "pk")
        .values_list("reference", flat=True)[:room]
    )
    return pending + list(late)


def reverify_school() -> dict[str, int]:
    """Re-verify this school's open attempts. Runs inside the school's schema.

    A gateway error skips that attempt (only its exception type is logged); three in a row stop
    the run, since the gateway is probably down and the next sweep is 15 minutes away.

    A database error on one attempt (a deadlock, say) is logged by type and the sweep goes on.
    Inside an outer transaction each attempt runs in its own savepoint so the error does not
    poison the rest; outside one, ``process_attempt``'s own atomic blocks roll back, and no
    transaction is held open across its gateway call.
    """
    checked = errors = consecutive = 0
    for reference in sweep_references():
        try:
            with transaction.atomic() if connection.in_atomic_block else nullcontext():
                process_attempt(reference, abandon_stale=True)
        except DatabaseError as exc:
            errors += 1
            logger.warning("Re-verification skipped an attempt (%s).", type(exc).__name__)
            continue
        except GatewayUnavailable as exc:
            errors += 1
            consecutive += 1
            logger.warning("Re-verification skipped an attempt (%s).", type(exc).__name__)
            if consecutive >= MAX_CONSECUTIVE_GATEWAY_ERRORS:
                logger.warning("Re-verification stopped early after repeated gateway errors.")
                break
            continue
        consecutive = 0
        checked += 1
    return {"checked": checked, "errors": errors}


def clean_note(note: str) -> str:
    note = (note or "").strip()
    if not note:
        raise ValidationError("Enter a note explaining how this was resolved.")
    if len(note) > NOTE_MAX:
        raise ValidationError(f"The note must be at most {NOTE_MAX} characters.")
    return note


def mark_resolved(payment: Payment, *, note: str, by, request=None) -> Payment:
    """Clear a flagged payment after the bursar dealt with the excess outside the system.

    ``unallocated_kobo`` is kept for the record. The audit event carries ids and amounts only,
    never the note.
    """
    note = clean_note(note)
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if not locked.flagged:
            raise PaymentNotFlaggedError("This payment is no longer flagged.")
        locked.flagged = False
        locked.resolution_note = note
        locked.resolved_by = by
        locked.resolved_at = timezone.now()
        locked.save(update_fields=["flagged", "resolution_note", "resolved_by", "resolved_at"])
        record(
            "fees.payment_resolved",
            request=request,
            actor=by,
            obj=locked,
            after={"payment_id": locked.pk, "unallocated_kobo": locked.unallocated_kobo},
        )
    return locked
