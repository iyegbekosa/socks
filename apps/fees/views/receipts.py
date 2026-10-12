import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.roles import require_roles
from apps.core.parsing import parse_id
from apps.fees import receipts
from apps.fees.models import Receipt
from apps.fees.scope import MANAGE_ROLES, can_view_fees
from apps.fees.tasks import generate_receipt_task

logger = logging.getLogger(__name__)


@never_cache
def receipt_download(request, receipt_pk):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), str(settings.LOGIN_URL))
    receipt = receipts.downloadable_receipt(request.user, receipt_pk)
    if receipt is None:
        raise Http404
    if not receipt.pdf_name:
        return render(
            request,
            "fees/receipt_pending.html",
            {"receipt": receipt, "can_view": can_view_fees(request.user)},
        )
    response = HttpResponseRedirect(default_storage.url(receipt.pdf_name))
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_roles(*MANAGE_ROLES)
@require_POST
def receipt_regenerate(request, receipt_pk):
    pk = parse_id(str(receipt_pk))
    if pk is None:
        raise Http404
    receipt = get_object_or_404(Receipt, pk=pk)
    try:
        generate_receipt_task.apply_async(args=[receipt.payment_id], queue="reports")
    except Exception as exc:  # noqa: BLE001 - tell the user instead of a 500
        logger.warning(
            "Could not queue receipt %s for regeneration (%s).", receipt.pk, type(exc).__name__
        )
        messages.error(request, "The receipt could not be queued. Please try again.")
    else:
        messages.success(request, "The receipt is being regenerated.")
    return _back(request, receipt)


def _back(request, receipt):
    first = receipt.payment.allocations.order_by("pk").first()
    if first is not None:
        return redirect("fees:invoice", invoice_id=first.invoice_id)
    return redirect("fees:invoices")
