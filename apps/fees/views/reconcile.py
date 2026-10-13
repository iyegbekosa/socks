"""Reconciliation (§8.1): flagged attempts and payments, pending approvals, missing receipts."""

import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.roles import require_roles
from apps.core.parsing import parse_id
from apps.fees import reconcile
from apps.fees.confirm import process_attempt
from apps.fees.gateways import GatewayUnavailable
from apps.fees.models import ManualPayment, Payment, PaymentAttempt, Receipt
from apps.fees.scope import MANAGE_ROLES, VIEW_ROLES, can_manage_fees
from apps.fees.views.setup import _require_manage

logger = logging.getLogger(__name__)

RECHECKABLE = (
    PaymentAttempt.FLAGGED,
    PaymentAttempt.PENDING,
    PaymentAttempt.FAILED,
    PaymentAttempt.ABANDONED,
)


def _page(request, *, status: int = 200, resolve_errors=None):
    attempts = PaymentAttempt.objects.filter(status=PaymentAttempt.FLAGGED).select_related(
        "student__user"
    )
    payments = Payment.objects.filter(flagged=True).select_related("receipt", "student__user")
    missing = Receipt.objects.filter(pdf_name="").select_related("payment")
    pending = ManualPayment.objects.filter(status=ManualPayment.PENDING).aggregate(n=Count("pk"))
    return render(
        request,
        "fees/reconcile.html",
        {
            "attempts": attempts,
            "payments": payments,
            "missing_receipts": missing,
            "pending_count": pending["n"],
            "can_act": can_manage_fees(request.user),
            "resolve_errors": resolve_errors or {},
        },
        status=status,
    )


def _get_or_404(model, raw):
    pk = parse_id(str(raw))
    if pk is None:
        raise Http404
    return get_object_or_404(model, pk=pk)


@never_cache
@require_roles(*VIEW_ROLES)
def reconcile_page(request):
    return _page(request)


@never_cache
@require_roles(*MANAGE_ROLES)
@require_POST
def payment_resolve(request, payment_id):
    _require_manage(request)
    payment = _get_or_404(Payment, payment_id)
    try:
        reconcile.mark_resolved(
            payment, note=request.POST.get("note", ""), by=request.user, request=request
        )
    except reconcile.PaymentNotFlaggedError as exc:
        messages.error(request, exc.messages[0])
        return _page(request, status=409)
    except ValidationError as exc:
        return _page(request, status=400, resolve_errors={payment.pk: exc.messages})
    messages.success(request, "Payment marked as resolved.")
    return redirect("fees:reconcile")


@never_cache
@require_roles(*MANAGE_ROLES)
@require_POST
def attempt_recheck(request, attempt_id):
    _require_manage(request)
    attempt = _get_or_404(PaymentAttempt, attempt_id)
    if attempt.status not in RECHECKABLE:
        messages.info(request, "This payment attempt is already confirmed.")
        return redirect("fees:reconcile")
    try:
        status = process_attempt(attempt.reference)
    except GatewayUnavailable as exc:
        logger.warning("Manual re-check failed (%s).", type(exc).__name__)
        messages.error(
            request, "We could not reach the payment provider just now. Please try again shortly."
        )
    else:
        label = dict(PaymentAttempt.STATUS_CHOICES).get(status, status)
        messages.success(request, f"Re-checked. The attempt status is now: {label}.")
    return redirect("fees:reconcile")
