"""Receipt PDFs (FR-14): render, store, message guardians and authorise downloads.

A receipt PDF is stored privately (``default_storage``) at ``receipts/<year>/<receipt pk>.pdf``
and overwritten on regeneration. Logs carry ids and exception types only.
"""

import logging
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.db.models import Q, QuerySet
from django.template.loader import render_to_string

from apps.branding.models import get_branding
from apps.core.logging import log_exception_redacted
from apps.core.money import naira_display
from apps.core.parsing import parse_id
from apps.core.pdf import render_pdf
from apps.core.sms import send_sms, sms_configured
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


def _unique(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = (value or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def send_receipt(payment_id: int) -> dict[str, int]:
    """Email (and, when configured, text) the student's guardians that a payment was received.
    No PDF and no balances; recipients are de-duplicated case-insensitively. Failures are
    logged by type and never raised."""
    counts = {"emails_sent": 0, "sms_queued": 0, "failures": 0}
    receipt = _receipt_for_payment(payment_id)
    if receipt is None:
        return counts
    student = receipt.payment.student
    body = MESSAGE.format(
        amount=naira_display(receipt.payment.amount_kobo),
        first_name=student.first_name,
        number=receipt.number,
    )
    emails = _unique([student.guardian_email, student.guardian2_email])
    phones = _unique([student.guardian_phone, student.guardian2_phone])
    for address in emails:
        try:
            send_mail(
                subject="Payment received",
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[address],
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001 - one bad address must not stop the rest
            counts["failures"] += 1
            logger.warning(
                "Receipt email for payment %s failed (%s).", payment_id, type(exc).__name__
            )
        else:
            counts["emails_sent"] += 1
    if sms_configured():
        for phone in phones:
            try:
                send_sms(phone, body)
            except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
                counts["failures"] += 1
                logger.warning(
                    "Receipt SMS for payment %s failed (%s).", payment_id, type(exc).__name__
                )
            else:
                counts["sms_queued"] += 1
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
