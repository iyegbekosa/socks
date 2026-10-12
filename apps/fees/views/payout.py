"""School payout account (FR-11, school_admin only) and the DEBUG-only fake checkout page.

Flow: pick a bank and enter the account number → "Check account" resolves the account name
and keeps the pending details in the session only → "Confirm" creates or updates the Paystack
subaccount. The database keeps the bank name and the last 4 digits, never the full number.
"""

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from apps.accounts.roles import SCHOOL_ADMIN, require_roles
from apps.fees import services
from apps.fees.forms import PayoutAccountForm
from apps.fees.gateways import UNAVAILABLE_MESSAGE, GatewayRejected, GatewayUnavailable, get_gateway
from apps.tenancy.limits import _current_school

PENDING_KEY = "fees_payout_pending"


def _schema() -> str:
    return str(getattr(connection, "schema_name", ""))


def _pending(request) -> dict[str, Any] | None:
    pending = request.session.get(PENDING_KEY)
    if not isinstance(pending, dict) or pending.get("schema") != _schema():
        return None
    return pending


def _clear_pending(request) -> None:
    request.session.pop(PENDING_KEY, None)


@never_cache
@require_roles(SCHOOL_ADMIN)
def payout(request):
    school = _current_school()
    if school is None:
        raise Http404
    error = ""
    gateway = None
    banks: list[Any] = []
    try:
        gateway = get_gateway()
        banks = gateway.banks()
    except GatewayUnavailable:
        error = UNAVAILABLE_MESSAGE

    action = request.POST.get("action") if request.method == "POST" else None
    form = PayoutAccountForm(banks=banks)

    if action == "cancel":
        _clear_pending(request)
        return redirect("fees:payout")

    if action == "confirm":
        pending = _pending(request)
        if pending is None:
            messages.error(request, "Check the account before confirming.")
            return redirect("fees:payout")
        if gateway is not None:
            try:
                services.save_payout_account(
                    gateway,
                    bank_code=pending["bank_code"],
                    bank_name=pending["bank_name"],
                    account_number=pending["account_number"],
                    by=request.user,
                    request=request,
                )
            except GatewayUnavailable:
                error = UNAVAILABLE_MESSAGE
            else:
                _clear_pending(request)
                messages.success(request, "Payout account saved. Online payments are on.")
                return redirect("fees:payout")

    elif action == "check" and gateway is not None and not error:
        _clear_pending(request)
        form = PayoutAccountForm(request.POST, banks=banks)
        if form.is_valid():
            bank_code = form.cleaned_data["bank_code"]
            account_number = form.cleaned_data["account_number"]
            try:
                account_name = gateway.resolve_account(account_number, bank_code)
            except GatewayRejected as exc:
                form.add_error("account_number", exc.message)
            except GatewayUnavailable:
                error = UNAVAILABLE_MESSAGE
            else:
                request.session[PENDING_KEY] = {
                    "schema": _schema(),
                    "bank_code": bank_code,
                    "bank_name": form.bank_names[bank_code],
                    "account_number": account_number,
                    "account_name": account_name,
                }

    pending = _pending(request)
    return render(
        request,
        "fees/payout.html",
        {
            "school": school,
            "form": form,
            "banks_available": bool(banks),
            "error": error,
            "pending": pending,
            "pending_last4": pending["account_number"][-4:] if pending else "",
        },
    )


@never_cache
@login_required
def fake_checkout(request, reference: str):
    """DEBUG only: shows what the FakeGateway recorded. Task 8 adds the pay/fail buttons."""
    if not settings.DEBUG:
        raise Http404
    from apps.fees.gateways.fake import FakeGateway

    state = FakeGateway.lookup(reference)
    if state is None:
        raise Http404
    return render(
        request,
        "fees/fake_checkout.html",
        {"reference": reference, "amount_kobo": state.get("amount_kobo") or 0, "state": state},
    )
