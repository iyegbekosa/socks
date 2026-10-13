"""Online checkout (§4.5 steps 1–3): a student starts paying their own or a sibling's invoices.

``start`` validates the plan, then in one transaction creates the ``PaymentAttempt``, its
lines and the public ``PaymentRoute`` (invoice rows locked ``FOR UPDATE`` in pk order, the
same order as the ledger). The gateway is called after commit, outside any transaction or
lock. Nothing here credits an invoice: confirmation is the webhook's job (Task 8), and the
return page only reads the attempt's status.
"""

import base64
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.http import Http404

from apps.audit.services import record as audit
from apps.core.money import naira_display
from apps.fees import siblings
from apps.fees.gateways import GatewayUnavailable, get_gateway
from apps.fees.ledger import PlannedLine
from apps.fees.models import FeeSettings, Invoice, PaymentAttempt, PaymentAttemptLine
from apps.students.models import Student
from apps.tenancy.limits import _current_school
from apps.tenancy.models import PaymentRoute

logger = logging.getLogger(__name__)

REFERENCE_PREFIX = "B2L-"
REFERENCE_RETRIES = 3  # after the first try, on the (very unlikely) unique collision
MAX_REFERENCE_LENGTH = 40
MAX_LINES = 50

FLAG_UNAVAILABLE_AT_START = "Gateway unavailable at start"
MSG_NOT_READY = "This school has not set up online payments yet."
MSG_GATEWAY_DOWN = (
    "Online payment is not available right now. You can pay by bank transfer and give the "
    "teller to the school bursar."
)
MSG_EMPTY = "Choose at least one invoice to pay."
MSG_TOO_MANY = f"Choose at most {MAX_LINES} invoices."
MSG_DUPLICATE = "Choose each invoice once."
MSG_AMOUNT = "Enter a valid amount for each invoice."
MSG_LINE = "Each invoice amount must be more than zero."
MSG_CARRIED = "An invoice whose balance was carried forward cannot be paid. Pay the newest one."
MSG_SETTLED = "That invoice has nothing left to pay."
MSG_OVER_BALANCE = "An invoice amount is more than that invoice's balance."
MSG_FULL_BALANCE = "Pay the full balance of each invoice."
MSG_BELOW_MINIMUM = "Pay at least {minimum} on each invoice, or its whole balance if smaller."

STATUSES = frozenset(code for code, _ in PaymentAttempt.STATUS_CHOICES)


class OnlinePaymentsNotReady(Exception):
    """The school has no payout account (Paystack subaccount) yet."""


def new_reference() -> str:
    """``B2L-`` and 20 uppercase base32 characters (100 random bits) from ``secrets``."""
    return REFERENCE_PREFIX + base64.b32encode(secrets.token_bytes(13)).decode()[:20]


def customer_email(student: Student) -> str:
    """The gateway customer email. Never logged or audited."""
    if student.guardian_email:
        return student.guardian_email
    if student.guardian2_email:
        return student.guardian2_email
    schema = getattr(connection, "schema_name", "")
    return f"pay+{schema}-{student.pk}@{settings.PAYMENT_PLACEHOLDER_EMAIL_DOMAIN}"


def payable_invoices(student: Student) -> list[Invoice]:
    """The student's and their siblings' invoices with a balance, oldest term first."""
    owners = {student.pk, *(s.pk for s in siblings.siblings_of(student))}
    invoices = (
        Invoice.objects.filter(student_id__in=owners)
        .exclude(status=Invoice.CARRIED)
        .select_related("student", "term__session")
    )
    return [inv for inv in invoices if inv.balance_kobo > 0]


def _check_shape(lines: list[PlannedLine]) -> None:
    if not lines:
        raise ValidationError(MSG_EMPTY)
    if len(lines) > MAX_LINES:
        raise ValidationError(MSG_TOO_MANY)
    if len({line.invoice_id for line in lines}) != len(lines):
        raise ValidationError(MSG_DUPLICATE)
    for line in lines:
        kobo = line.amount_kobo
        if isinstance(kobo, bool) or not isinstance(kobo, int):
            raise ValidationError(MSG_AMOUNT)
        if kobo <= 0:
            raise ValidationError(MSG_LINE)


def _locked_invoices(student: Student, lines: list[PlannedLine]) -> dict[int, Invoice]:
    """Lock the planned invoices (pk order). A foreign or unknown id is a 404."""
    owners = {student.pk, *(s.pk for s in siblings.siblings_of(student))}
    ids = sorted({line.invoice_id for line in lines})
    invoices = {
        inv.pk: inv
        for inv in Invoice.objects.select_for_update()
        .filter(pk__in=ids, student_id__in=owners)
        .order_by("pk")
    }
    if len(invoices) != len(ids):
        raise Http404("Invoice not found.")
    return invoices


def _check_amounts(lines: list[PlannedLine], invoices: dict[int, Invoice]) -> None:
    conf = FeeSettings.load()
    for line in lines:
        invoice = invoices[line.invoice_id]
        balance = invoice.balance_kobo
        if invoice.status == Invoice.CARRIED:
            raise ValidationError(MSG_CARRIED)
        if balance <= 0:
            raise ValidationError(MSG_SETTLED)
        if not conf.allow_part_payment:
            if line.amount_kobo != balance:
                raise ValidationError(MSG_FULL_BALANCE)
            continue
        if line.amount_kobo > balance:
            raise ValidationError(MSG_OVER_BALANCE)
        if line.amount_kobo < min(conf.min_part_payment_kobo, balance):
            raise ValidationError(
                MSG_BELOW_MINIMUM.format(minimum=naira_display(conf.min_part_payment_kobo))
            )


def _create_attempt(student: Student, amount_kobo: int, schema: str) -> PaymentAttempt:
    """Attempt + public route under a fresh reference, retrying on a unique collision."""
    for tries_left in range(REFERENCE_RETRIES, -1, -1):
        reference = new_reference()
        try:
            with transaction.atomic():
                attempt = PaymentAttempt.objects.create(
                    reference=reference, student=student, amount_kobo=amount_kobo
                )
                PaymentRoute.objects.create(reference=reference, schema_name=schema)
            return attempt
        except IntegrityError:
            if tries_left == 0:
                raise
            logger.warning("Payment reference collision; generating another.")
    raise AssertionError("unreachable")  # pragma: no cover


def start(
    student: Student,
    lines: list[PlannedLine],
    *,
    callback_url: str,
    request: Any = None,
) -> PaymentAttempt:
    """Start an online payment and return the attempt, with ``authorization_url`` set.

    Raises ``OnlinePaymentsNotReady`` (no payout account), ``Http404`` (an invoice that is
    not the student's or a sibling's), ``ValidationError`` (a user-facing message) or
    ``GatewayUnavailable`` (the attempt is then ``failed``). ``callback_url`` is the absolute
    return page URL; ``?reference=`` is appended.
    """
    school = _current_school()
    if school is None or not school.online_payments_ready:
        raise OnlinePaymentsNotReady
    lines = list(lines)
    _check_shape(lines)
    schema = str(getattr(connection, "schema_name", ""))

    with transaction.atomic():
        invoices = _locked_invoices(student, lines)
        _check_amounts(lines, invoices)
        # Construct (no network) before writing, so a misconfigured gateway leaves no attempt.
        gateway = get_gateway()
        total = sum(line.amount_kobo for line in lines)
        attempt = _create_attempt(student, total, schema)
        PaymentAttemptLine.objects.bulk_create(
            PaymentAttemptLine(
                attempt=attempt, invoice_id=line.invoice_id, amount_kobo=line.amount_kobo
            )
            for line in lines
        )
        audit(
            "fees.attempt_started",
            request=request,
            actor=None if request is not None else student.user,
            obj=attempt,
            after={
                "attempt_id": attempt.pk,
                "amount_kobo": total,
                "invoice_ids": [line.invoice_id for line in lines],
            },
        )

    separator = "&" if "?" in callback_url else "?"
    try:
        result = gateway.initialize(
            email=customer_email(student),
            amount_kobo=total,
            reference=attempt.reference,
            subaccount=school.paystack_subaccount_code,
            callback_url=f"{callback_url}{separator}{urlencode({'reference': attempt.reference})}",
            metadata={"schema": schema, "attempt_id": attempt.pk},
        )
    except GatewayUnavailable as exc:
        logger.warning(
            "Gateway initialize failed for attempt %s (%s).", attempt.pk, type(exc).__name__
        )
        attempt.status = PaymentAttempt.FAILED
        attempt.flag_reason = FLAG_UNAVAILABLE_AT_START
        attempt.save(update_fields=["status", "flag_reason", "updated_at"])
        raise
    attempt.authorization_url = result.authorization_url
    return attempt


def status_for(student: Student, reference: Any) -> str:
    """The attempt's status for the return page. Read only: never calls the gateway.

    Raises ``Http404`` unless ``reference`` is one of ``student``'s own attempts."""
    if not isinstance(reference, str) or not 0 < len(reference) <= MAX_REFERENCE_LENGTH:
        raise Http404
    status = (
        PaymentAttempt.objects.filter(reference=reference, student=student)
        .values_list("status", flat=True)
        .first()
    )
    if status is None or status not in STATUSES:
        raise Http404
    return str(status)


def unavailable_message() -> str:
    """The gateway-down message with the school's bank transfer note, if any."""
    conf = FeeSettings.objects.filter(pk=1).first()
    note = (conf.bank_transfer_note if conf is not None else "").strip()
    return f"{MSG_GATEWAY_DOWN} {note}" if note else MSG_GATEWAY_DOWN
