import math

from django.conf import settings
from django.contrib import messages
from django.db import connection
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from apps.accounts.forms import ResetRequestForm, ResetVerifyForm
from apps.accounts.identifiers import classify_identifier
from apps.accounts.models import User
from apps.accounts.services import (
    clear_reset_code,
    find_user_by_identifier,
    issue_reset_code,
    reset_phone_for,
    verify_reset_code,
)
from apps.accounts.views.login import id_limit as login_id_limit
from apps.audit.services import record
from apps.core.ratelimit import RateLimit
from apps.core.request_id import client_ip
from apps.core.sms import send_sms

SESSION_KEY = "reset_identifier"
SENT_MESSAGE = "If an account matches, we've sent a code to its registered phone."
BAD_CODE = "That code is wrong or has expired."

id_limit = RateLimit("reset:id")
ip_limit = RateLimit("reset:ip")


@never_cache
def reset_request_view(request):
    if request.method != "POST":
        return render(request, "accounts/reset_request.html", {"form": ResetRequestForm()})
    form = ResetRequestForm(request.POST)
    if not form.is_valid():
        return render(request, "accounts/reset_request.html", {"form": form})
    ident = classify_identifier(form.cleaned_data["identifier"])[1]
    id_key = f"reset:id:{ident}"
    ip = client_ip(request)
    ip_key = f"reset:ip:{ip}" if ip else None

    # Reserve first: every request counts, whether or not the account exists.
    id_count = id_limit.hit(id_key)
    ip_count = ip_limit.hit(ip_key) if ip_key else 0
    waits = []
    if id_count > id_limit.limit:
        waits.append(id_limit.retry_after(id_key))
    if ip_key and ip_count > ip_limit.limit:
        waits.append(ip_limit.retry_after(ip_key))
    if waits:
        if ip_key and ip_count <= ip_limit.limit:
            ip_limit.release(ip_key)
        if id_count <= id_limit.limit:
            id_limit.release(id_key)
        minutes = max(1, math.ceil(max(waits) / 60))
        unit = "minute" if minutes == 1 else "minutes"
        return render(
            request,
            "accounts/reset_request.html",
            {"form": form, "error": f"Too many requests. Try again in {minutes} {unit}."},
            status=429,
        )

    user = find_user_by_identifier(ident)
    if user is not None and reset_phone_for(user):
        code = issue_reset_code(user)
        if code:
            send_sms(
                reset_phone_for(user),  # type: ignore[arg-type]
                f"{connection.tenant.name}: your password reset code is {code}. "
                "It expires in 10 minutes.",
            )
    request.session[SESSION_KEY] = ident
    messages.info(request, SENT_MESSAGE)
    return redirect(reverse("accounts:reset_verify"))


@never_cache
def reset_verify_view(request):
    ident = request.session.get(SESSION_KEY)
    if not ident:
        return redirect(reverse("accounts:reset"))
    found = find_user_by_identifier(ident)
    # A throwaway instance keeps the response identical when the account does not exist.
    form = ResetVerifyForm(found or User(), request.POST or None)
    if request.method != "POST" or not form.is_valid():
        return render(request, "accounts/reset_verify.html", {"form": form})
    if found is None or not verify_reset_code(found, form.cleaned_data["code"]):
        form = ResetVerifyForm(found or User())
        return render(request, "accounts/reset_verify.html", {"form": form, "error": BAD_CODE})
    form.save()  # set_password + save
    clear_reset_code(found)
    request.session.pop(SESSION_KEY, None)
    record("auth.password_reset", request=request, actor=found, obj=found)
    # A locked-out user must be able to use the new password straight away.
    login_id_limit.reset(f"login:id:{ident}")
    messages.success(request, "Your password has been changed. Log in with the new password.")
    return redirect(settings.LOGIN_URL)
