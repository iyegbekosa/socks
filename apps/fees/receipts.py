"""Receipt PDFs (FR-14): render, store, message guardians and authorise downloads.

A receipt PDF is stored privately (``default_storage``) at ``receipts/<year>/<receipt pk>.pdf``
and overwritten on regeneration. Logs carry ids and exception types only.
"""

import logging
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q, QuerySet
from django.template.loader import render_to_string

from apps.branding.models import get_branding
from apps.comms.models import Message
from apps.comms.render import literal
from apps.comms.service import notify, record_notify_failure
from apps.core.logging import log_exception_redacted
from apps.core.money import naira_display
from apps.core.parsing import parse_id
from apps.core.pdf import render_pdf
from apps.fees.models import Receipt
from apps.fees.scope import can_view_fees
from apps.results.cards import _school_name, load_logo
from apps.students.models import Student

logger = logging.getLogger(__name__)

MESSAGE = "Payment of {amount} received for {first_name}. Receipt {number}."


def receipt_name(receipt: Receipt) -> str:
    return f"receipts/{receipt.year}/{receipt.pk}.pdf"


def _reference(payment: Any) -> str:
    """The gateway reference, or the teller/reference number of a manual payment."""
    if payment.gateway_reference:
        return payment.gateway_reference
    if payment.manual_id is not None:
        return payment.manual.reference_no
    return ""


def _receipt_for_payment(payment_id: int) -> Receipt | None:
    return (
        Receipt.objects.select_related(
            "payment__student__user", "payment__recorded_by", "payment__manual"
        )
        .filter(payment_id=payment_id)
        .first()
    )


def receipt_context(receipt: Receipt, *, logo: str | None) -> dict[str, Any]:
    payment = receipt.payment
    student = payment.student
    branding = get_branding()
    lines = [
        {
            "term": str(al.invoice.term),
            "amount": naira_display(al.amount_kobo),
            # The balance just after this payment (Ruling T); older allocations fall back to
            # the current balance.
            "balance": naira_display(
                al.balance_after_kobo
                if al.balance_after_kobo is not None
                else al.invoice.balance_kobo
            ),
        }
        for al in payment.allocations.select_related("invoice__term__session").order_by("pk")
    ]
    recorder = payment.recorded_by
    return {
        "school_name": _school_name(),
        "motto": branding.motto,
        "address": branding.address,
        "primary_color": branding.primary_color,
        "logo": logo,
        "number": receipt.number,
        "paid_at": payment.paid_at,
        "student_name": student.full_name,
        "admission_number": student.admission_number or "",
        "method": payment.get_method_display(),
        "reference": _reference(payment),
        "lines": lines,
        "total": naira_display(payment.amount_kobo),
        "unallocated": naira_display(payment.unallocated_kobo) if payment.unallocated_kobo else "",
        "recorded_by": (recorder.get_full_name() or recorder.username) if recorder else "",
        "online": recorder is None and payment.method == payment.ONLINE,
    }


def render_receipt(receipt: Receipt) -> bytes:
    html = render_to_string("fees/receipt.html", receipt_context(receipt, logo=load_logo()))
    return render_pdf(html)


# Outcomes of ``store_receipt``.
FAILED, STORED, FIRST_STORED = "failed", "stored", "first_stored"


def store_receipt(payment_id: int) -> str:
    """Render and store the payment's receipt PDF. Returns ``FIRST_STORED`` when this run set
    ``pdf_name`` for the first time (a conditional update, so of two racing runs only one gets
    it), ``STORED`` when it replaced an existing PDF and ``FAILED`` otherwise. Never raises: a
    failure is logged by type and leaves ``pdf_name`` as it was ("Regenerate" retries)."""
    try:
        receipt = _receipt_for_payment(payment_id)
        if receipt is None:
            return FAILED
        pdf = render_receipt(receipt)
        name = default_storage.save(receipt_name(receipt), ContentFile(pdf))
        if Receipt.objects.filter(pk=receipt.pk, pdf_name="").update(pdf_name=name):
            return FIRST_STORED
        Receipt.objects.filter(pk=receipt.pk).update(pdf_name=name)
        return STORED
    except Exception:  # noqa: BLE001 - the receipt stays without a PDF
        log_exception_redacted(logger, "Receipt PDF for payment %s failed", payment_id)
        return FAILED


def generate_receipt(payment_id: int) -> bool:
    """Render and store the payment's receipt PDF. True when stored; see ``store_receipt``."""
    return store_receipt(payment_id) != FAILED


def receipt_dedupe_key(receipt: Receipt) -> str:
    return f"receipt:{receipt.pk}"


def send_receipt(payment_id: int) -> dict[str, int]:
    """Tell the student's family (email) and the student (in-app) that a payment was received,
    through ``comms.notify``. Email and in-app only: no SMS (Ruling AF). No PDF and no balances;
    email recipients are de-duplicated case-insensitively. Receipts ignore quiet hours. Sends
    once per receipt (``dedupe_key`` "receipt:<pk>"): a second run, such as a redelivered
    task, finds the message and stops. Failures are logged (message redacted), audited as
    ``comms.notify_failed`` and never raised."""
    counts = {"emails_queued": 0, "inapp": 0, "failures": 0}
    receipt = _receipt_for_payment(payment_id)
    if receipt is None:
        return counts
    student = receipt.payment.student
    key = receipt_dedupe_key(receipt)
    # ``{amount}`` and ``{number}`` are not message placeholders: render them in first (escaped)
    # and leave ``{student_first_name}`` for ``notify``. The text is the P4 text, unchanged.
    template = MESSAGE.format(
        amount=literal(naira_display(receipt.payment.amount_kobo)),
        first_name="{student_first_name}",
        number=literal(receipt.number),
    )
    try:
        with transaction.atomic():
            Receipt.objects.select_for_update().filter(pk=receipt.pk).first()
            if Message.objects.filter(kind=Message.Kind.RECEIPT, dedupe_key=key).exists():
                return counts
            message = notify(
                "receipt",
                students=[student],
                subject="Payment received",
                body=template,
                per_student=True,
                context_for=lambda s: {"student_first_name": s.first_name},
                related=receipt,
                audience_label="1 student",
                dedupe_key=key,
            )
    except Exception:  # noqa: BLE001 - a receipt message must never raise
        counts["failures"] += 1
        log_exception_redacted(logger, "Receipt message for payment %s failed", payment_id)
        record_notify_failure(Message.Kind.RECEIPT.value, receipt.pk)
        return counts
    counts["emails_queued"] = message.deliveries.filter(channel="email").count()
    counts["inapp"] = message.deliveries.filter(channel="inapp").count()
    return counts


def receipts_for_student(student: Student) -> QuerySet[Receipt]:
    """Receipts a student may open: their own payments', or a payment allocated to one of
    their invoices. The one definition of the student rule (download check and portal)."""
    return Receipt.objects.filter(
        Q(payment__student=student) | Q(payment__allocations__invoice__student=student)
    ).distinct()


def downloadable_receipt(user: Any, raw_pk: Any) -> Receipt | None:
    """The receipt this user may open, or None (404). Staff who can view fees: any. Students:
    receipts of their own payments, or of a payment allocated to one of their invoices."""
    pk = parse_id(str(raw_pk))
    if pk is None or user is None or not getattr(user, "is_authenticated", False):
        return None
    if not user.is_active:
        return None
    receipt = Receipt.objects.select_related("payment").filter(pk=pk).first()
    if receipt is None:
        return None
    if can_view_fees(user):
        return receipt
    if getattr(user, "kind", None) != "student":
        return None
    student = Student.objects.filter(user=user).first()
    if student is None:
        return None
    if receipts_for_student(student).filter(pk=receipt.pk).exists():
        return receipt
    return None
