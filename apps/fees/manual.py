"""Manual payments (UC-13): cash, teller, POS and transfer payments recorded by staff.

A payment at or above ``FeeSettings.manual_approval_threshold_kobo`` waits for a second fee
manager (approver != recorder) and credits nothing until approved. Below it, the payment is
confirmed at once with ``approved_by = recorded_by``. Only ``ledger.confirm_payment`` creates
the Payment.

Lock order: invoice rows (pk order, in ``record``) or the ManualPayment row (in ``approve`` and
``reject``), then whatever ``confirm_payment`` takes (invoices by pk, then the counter).
"""

import logging
import os
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record as audit
from apps.core.images import clean_image
from apps.core.storage import unique_upload_name
from apps.fees import ledger
from apps.fees.ledger import PlannedLine
from apps.fees.models import FeeSettings, Invoice, ManualPayment, Payment
from apps.fees.scope import can_manage_fees
from apps.students.models import Student
from apps.tenancy.limits import PlanLimits

logger = logging.getLogger(__name__)

MAX_KOBO = 10_000_000_000  # ₦100,000,000.00
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_SIDE = 6000  # pixels; a tiny compressed file can declare a huge canvas
IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
PHOTO_EXTENSIONS = (*IMAGE_FORMATS, ".pdf")
METHODS = frozenset(code for code, _ in ManualPayment.METHOD_CHOICES)
NEEDS_REFERENCE = frozenset({ManualPayment.TELLER, ManualPayment.POS, ManualPayment.TRANSFER})
MAX_PLAN_LINES = 50

MSG_REFERENCE = "Enter the teller or reference number."
MSG_FUTURE = "The date paid cannot be in the future."
MSG_SUM = "The invoice amounts must add up to the amount paid."
MSG_OWNER = "Choose only this student's invoices."
MSG_CARRIED = "An invoice whose balance was carried forward cannot be paid. Pay the newest one."
MSG_OVER_BALANCE = "An invoice amount is more than that invoice's balance."
MSG_LINE = "Each invoice amount must be more than zero."
MSG_EMPTY = "Choose at least one invoice and enter an amount for it."
MSG_PHOTO_TYPE = "Upload a JPEG, PNG or PDF file."
MSG_PHOTO_SIZE = "The photo must be 5 MB or smaller."
MSG_PHOTO_PDF = "That file is not a valid PDF."
MSG_REASON = "Give a reason for rejecting this payment."
MSG_SELF = "Another bursar or school admin must approve a payment you recorded."


class ManualStateError(ValidationError):
    """The manual payment was already approved or rejected."""


LAGOS = ZoneInfo("Africa/Lagos")


def _now_date() -> date:
    """Today in Lagos, whatever timezone is active."""
    return timezone.localtime(timezone.now(), LAGOS).date()


def paid_at_for(paid_on: date) -> datetime:
    """Noon, Africa/Lagos, on the day paid."""
    return datetime.combine(paid_on, time(12, 0), tzinfo=LAGOS)


# --- photos ---------------------------------------------------------------------------------


def clean_photo(upload: Any) -> ContentFile:
    """Check a teller photo by content (Pillow ``verify`` for JPEG/PNG, ``%PDF-`` magic for
    PDF) and size. Images are re-encoded from pixels, dropping metadata and trailing payloads.
    Raises ``ValidationError`` with a user-facing message."""
    name = os.path.basename(getattr(upload, "name", "") or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in PHOTO_EXTENSIONS:
        raise ValidationError(MSG_PHOTO_TYPE)
    size = getattr(upload, "size", None)
    if size is not None and size > MAX_PHOTO_BYTES:
        raise ValidationError(MSG_PHOTO_SIZE)
    if ext == ".pdf":
        upload.seek(0)
        raw = upload.read(MAX_PHOTO_BYTES + 1)
        upload.seek(0)
        if len(raw) > MAX_PHOTO_BYTES:
            raise ValidationError(MSG_PHOTO_SIZE)
        if not raw.startswith(b"%PDF-"):
            raise ValidationError(MSG_PHOTO_PDF)
        return ContentFile(raw, name="teller.pdf")
    # Same validate + re-encode step as logos and student photos; its size message reads
    # exactly MSG_PHOTO_SIZE with this label.
    return clean_image(
        upload,
        max_bytes=MAX_PHOTO_BYTES,
        allowed=IMAGE_FORMATS,
        max_pixels=MAX_PHOTO_SIDE * MAX_PHOTO_SIDE,
        output="same",
        max_side=None,
        quality=90,
        label="The photo",
    )


def _store_photo(photo: ContentFile) -> str:
    """Save privately under ``fees/tellers/``. Raises ``PlanLimitExceeded`` over quota."""
    PlanLimits.for_current_school().check_can_store(photo.size)
    return default_storage.save(unique_upload_name("fees/tellers", photo.name or ""), photo)


def _delete_photo(name: str) -> None:
    try:
        default_storage.delete(name)
    except Exception as exc:  # noqa: BLE001 - best effort; the record was not saved
        logger.warning("Could not delete an unused teller photo (%s).", type(exc).__name__)


# --- validation -----------------------------------------------------------------------------


def _check_fields(
    *, method: str, amount_kobo: Any, plan: list[PlannedLine], paid_on: Any, reference_no: str
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    if method not in METHODS:
        errors["method"] = ["Choose cash, teller, POS or transfer."]
    if isinstance(amount_kobo, bool) or not isinstance(amount_kobo, int) or amount_kobo <= 0:
        errors["amount"] = ["Enter an amount greater than zero."]
    elif amount_kobo > MAX_KOBO:
        errors["amount"] = ["Amount is too large."]
    if method in NEEDS_REFERENCE and not reference_no:
        errors["reference_no"] = [MSG_REFERENCE]
    if len(reference_no) > 64:
        errors["reference_no"] = ["Use at most 64 characters."]
    if not isinstance(paid_on, date) or isinstance(paid_on, datetime):
        errors["paid_on"] = ["Enter the date paid."]
    elif paid_on > _now_date():
        errors["paid_on"] = [MSG_FUTURE]
    if not plan:
        errors["invoices"] = [MSG_EMPTY]
    elif len(plan) > MAX_PLAN_LINES:
        errors["invoices"] = [f"Choose at most {MAX_PLAN_LINES} invoices."]
    elif len({line.invoice_id for line in plan}) != len(plan):
        errors["invoices"] = ["Choose each invoice once."]
    elif any(
        isinstance(line.amount_kobo, bool)
        or not isinstance(line.amount_kobo, int)
        or line.amount_kobo <= 0
        for line in plan
    ):
        errors["invoices"] = [MSG_LINE]
    elif "amount" not in errors and sum(line.amount_kobo for line in plan) != amount_kobo:
        errors["invoices"] = [MSG_SUM]
    return errors


def _check_invoices(student: Student, plan: list[PlannedLine], invoices: dict[int, Invoice]):
    """Ownership (the student only, until siblings exist), carried and balance checks."""
    for line in plan:
        invoice = invoices.get(line.invoice_id)
        if invoice is None or invoice.student_id != student.pk:
            return MSG_OWNER
    for line in plan:
        invoice = invoices[line.invoice_id]
        if invoice.status == Invoice.CARRIED:
            return MSG_CARRIED
        if line.amount_kobo > invoice.balance_kobo:
            return MSG_OVER_BALANCE
    return None


def _invoices(plan: list[PlannedLine], *, lock: bool) -> dict[int, Invoice]:
    qs = Invoice.objects.filter(pk__in=sorted({line.invoice_id for line in plan})).order_by("pk")
    if lock:
        qs = qs.select_for_update()
    return {inv.pk: inv for inv in qs}


# --- actions --------------------------------------------------------------------------------


def record(
    *,
    student: Student,
    method: str,
    amount_kobo: int,
    plan: list[PlannedLine],
    paid_on: date,
    reference_no: str,
    photo: Any = None,
    by: Any,
    request: Any = None,
) -> ManualPayment:
    """Record a manual payment. Raises ``ValidationError`` (keyed by method, amount, invoices,
    paid_on, reference_no or photo), ``PlanLimitExceeded`` (photo over the storage quota) or
    ``PermissionDenied`` (``by`` is not a fee manager)."""
    if not can_manage_fees(by):
        raise PermissionDenied
    reference_no = (reference_no or "").strip()
    plan = list(plan)
    errors = _check_fields(
        method=method,
        amount_kobo=amount_kobo,
        plan=plan,
        paid_on=paid_on,
        reference_no=reference_no,
    )
    clean = None
    if photo is not None:
        try:
            clean = clean_photo(photo)
        except ValidationError as exc:
            errors["photo"] = exc.messages
    if errors:
        raise ValidationError(errors)
    # Check before uploading, so a bad plan wastes no upload; checked again under the locks.
    problem = _check_invoices(student, plan, _invoices(plan, lock=False))
    if problem:
        raise ValidationError({"invoices": [problem]})

    photo_name = _store_photo(clean) if clean is not None else ""
    try:
        return _record_locked(
            student=student,
            method=method,
            amount_kobo=amount_kobo,
            plan=plan,
            paid_on=paid_on,
            reference_no=reference_no,
            photo_name=photo_name,
            by=by,
            request=request,
        )
    except BaseException:
        if photo_name:
            _delete_photo(photo_name)
        raise


def _record_locked(
    *,
    student: Student,
    method: str,
    amount_kobo: int,
    plan: list[PlannedLine],
    paid_on: date,
    reference_no: str,
    photo_name: str,
    by: Any,
    request: Any,
) -> ManualPayment:
    with transaction.atomic():
        problem = _check_invoices(student, plan, _invoices(plan, lock=True))
        if problem:
            raise ValidationError({"invoices": [problem]})
        threshold = FeeSettings.load().manual_approval_threshold_kobo
        immediate = amount_kobo < threshold
        mp = ManualPayment.objects.create(
            student=student,
            method=method,
            amount_kobo=amount_kobo,
            plan=[{"invoice_id": ln.invoice_id, "amount_kobo": ln.amount_kobo} for ln in plan],
            paid_on=paid_on,
            reference_no=reference_no,
            photo_name=photo_name,
            status=ManualPayment.APPROVED if immediate else ManualPayment.PENDING,
            recorded_by=by,
            threshold_kobo=threshold,
            decided_by=by if immediate else None,
            decided_at=timezone.now() if immediate else None,
        )
        audit(
            "fees.manual_recorded",
            request=request,
            actor=by,
            obj=mp,
            after={
                "manual_id": mp.pk,
                "student_id": student.pk,
                "method": method,
                "amount_kobo": amount_kobo,
                "status": mp.status,
                "invoices": mp.plan,
            },
        )
        if immediate:
            _confirm(mp, recorded_by=by, approved_by=by)
    return mp


def _plan(mp: ManualPayment) -> list[PlannedLine]:
    return [PlannedLine(int(ln["invoice_id"]), int(ln["amount_kobo"])) for ln in mp.plan]


def _confirm(mp: ManualPayment, *, recorded_by: Any, approved_by: Any) -> Payment:
    return ledger.confirm_payment(
        student=mp.student,
        amount_kobo=mp.amount_kobo,
        method=mp.method,
        plan=_plan(mp),
        paid_at=paid_at_for(mp.paid_on),
        recorded_by=recorded_by,
        approved_by=approved_by,
        manual=mp,
    )


def _lock_pending(mp: ManualPayment, by: Any) -> ManualPayment:
    """Lock the row and check the decision is allowed. Call inside ``atomic()``."""
    if not can_manage_fees(by):
        raise PermissionDenied
    locked = ManualPayment.objects.select_for_update().get(pk=mp.pk)
    if locked.recorded_by_id == by.pk:
        raise PermissionDenied(MSG_SELF)
    if locked.status != ManualPayment.PENDING:
        raise ManualStateError(f"This payment was already {locked.get_status_display().lower()}.")
    return locked


def approve(mp: ManualPayment, *, by: Any, request: Any = None) -> Payment:
    """A second fee manager approves: the ledger caps each line at the current balance and
    flags any excess. Raises ``PermissionDenied`` (recorder, or not a fee manager) or
    ``ManualStateError`` (already decided)."""
    with transaction.atomic():
        locked = _lock_pending(mp, by)
        payment = _confirm(locked, recorded_by=locked.recorded_by, approved_by=by)
        locked.status = ManualPayment.APPROVED
        locked.decided_by = by
        locked.decided_at = timezone.now()
        locked.save(update_fields=["status", "decided_by", "decided_at"])
        audit(
            "fees.manual_approved",
            request=request,
            actor=by,
            obj=locked,
            after={
                "manual_id": locked.pk,
                "payment_id": payment.pk,
                "amount_kobo": locked.amount_kobo,
            },
        )
    mp.refresh_from_db()
    return payment


def reject(mp: ManualPayment, *, reason: str, by: Any, request: Any = None) -> ManualPayment:
    """Reject with a reason, kept on the row only (never in the audit metadata).

    Permission and state are checked (under the row lock) before the reason, so a recorder
    gets ``PermissionDenied`` and a decided record ``ManualStateError`` whatever the reason."""
    reason = " ".join((reason or "").split())
    with transaction.atomic():
        locked = _lock_pending(mp, by)
        if not reason:
            raise ValidationError({"reason": [MSG_REASON]})
        if len(reason) > 500:
            raise ValidationError({"reason": ["Use at most 500 characters."]})
        locked.status = ManualPayment.REJECTED
        locked.decided_by = by
        locked.decided_at = timezone.now()
        locked.reject_reason = reason
        locked.save(update_fields=["status", "decided_by", "decided_at", "reject_reason"])
        audit(
            "fees.manual_rejected",
            request=request,
            actor=by,
            obj=locked,
            after={"manual_id": locked.pk, "amount_kobo": locked.amount_kobo},
        )
    mp.refresh_from_db()
    return mp
