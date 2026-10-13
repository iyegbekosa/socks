"""School payout account (FR-11, school_admin only).

Flow: pick a bank and enter the account number → "Check account" resolves the account name →
"Confirm" re-posts the number, which is validated and resolved again (the name must match the
one shown at check) before the Paystack subaccount is created or updated (Ruling R).

Neither the database nor the session keeps the full number. Between the two steps the session
holds only the bank code, the last 4 digits, the resolved name, an HMAC-SHA256 of the number
(keyed with ``SECRET_KEY``) and an expiry 15 minutes ahead. The database keeps the bank name
and the last 4 digits.
"""

import hashlib
import hmac
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db import connection
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.accounts.roles import SCHOOL_ADMIN, require_roles
from apps.fees import services
from apps.fees.forms import PayoutAccountForm, clean_nuban
from apps.fees.gateways import UNAVAILABLE_MESSAGE, GatewayRejected, GatewayUnavailable, get_gateway
from apps.tenancy.limits import _current_school

PENDING_KEY = "fees_payout_pending"
PENDING_TTL = timedelta(minutes=15)
BANKS_UNAVAILABLE = "Bank list unavailable, try again shortly."
MSG_EXPIRED = "This check has expired. Check the account again."
MSG_MISMATCH = "The account number does not match the one you checked. Check the account again."
MSG_NAME_CHANGED = "The account name has changed since you checked it. Check the account again."


def _schema() -> str:
    return str(getattr(connection, "schema_name", ""))


def number_hmac(number: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), number.encode(), hashlib.sha256).hexdigest()


def _pending(request) -> tuple[dict[str, Any] | None, bool]:
    """The pending check for this school and whether it had expired (then it is cleared)."""
    pending = request.session.get(PENDING_KEY)
    if not isinstance(pending, dict) or pending.get("schema") != _schema():
        return None, False
    if not isinstance(pending.get("expires_at"), int | float) or (
        pending["expires_at"] <= timezone.now().timestamp()
    ):
        _clear_pending(request)
        return None, True
    return pending, False


def _clear_pending(request) -> None:
    request.session.pop(PENDING_KEY, None)


def _confirm(request, gateway, pending: dict[str, Any], bank_names: dict[str, str]) -> str:
    """Check the re-posted number against the pending check and save. Returns an error message,
    or "" when saved. Raises ``GatewayUnavailable``."""
    number = clean_nuban(request.POST.get("account_number", ""))
    if number is None or not hmac.compare_digest(number_hmac(number), pending["number_hmac"]):
        return MSG_MISMATCH
    bank_code = pending["bank_code"]
    if bank_code not in bank_names:
        return BANKS_UNAVAILABLE
    try:
        name = gateway.resolve_account(number, bank_code)
    except GatewayRejected as exc:
        _clear_pending(request)
        return exc.message
    if name != pending["resolved_name"]:
        _clear_pending(request)
        return MSG_NAME_CHANGED
    services.save_payout_account(
        gateway,
        bank_code=bank_code,
        bank_name=bank_names[bank_code],
        account_number=number,
        by=request.user,
        request=request,
    )
    _clear_pending(request)
    return ""


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
    else:
        if not banks:
            error = BANKS_UNAVAILABLE

    action = request.POST.get("action") if request.method == "POST" else None
    form = PayoutAccountForm(banks=banks)
    check_error = confirm_error = confirm_number = ""

    if action == "cancel":
        _clear_pending(request)
        return redirect("fees:payout")

    if action == "confirm":
        pending, expired = _pending(request)
        if expired:
            check_error = MSG_EXPIRED
        elif pending is None:
            messages.error(request, "Check the account before confirming.")
            return redirect("fees:payout")
        elif gateway is not None:
            try:
                confirm_error = _confirm(request, gateway, pending, form.bank_names)
            except GatewayUnavailable:
                error = UNAVAILABLE_MESSAGE
            else:
                if not confirm_error:
                    messages.success(request, "Payout account saved. Online payments are on.")
                    return redirect("fees:payout")
            if confirm_error and _pending(request)[0] is None:
                check_error, confirm_error = confirm_error, ""  # start again from the check

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
                    "last4": account_number[-4:],
                    "resolved_name": account_name,
                    "number_hmac": number_hmac(account_number),
                    "expires_at": (timezone.now() + PENDING_TTL).timestamp(),
                }
                confirm_number = account_number

    pending, _ = _pending(request)
    return render(
        request,
        "fees/payout.html",
        {
            "school": school,
            "form": form,
            "banks_available": bool(banks),
            "error": error,
            "pending": pending,
            "pending_bank_name": form.bank_names.get(pending["bank_code"], "") if pending else "",
            "check_error": check_error,
            "confirm_error": confirm_error,
            "confirm_number": confirm_number,
        },
    )
