import math

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from apps.accounts.forms import ResetRequestForm, ResetVerifyForm
from apps.accounts.identifiers import classify_identifier
from apps.accounts.models import User
from apps.accounts.services import (
    check_reset_code,
    consume_reset_code,
    find_user_by_identifier,
)
from apps.accounts.tasks import send_password_reset
from apps.accounts.views.login import forget_login_lockout
from apps.audit.services import record
from apps.core.ratelimit import RateLimit
from apps.core.request_id import client_ip, get_request_id
from apps.core.sms import sms_configured

SESSION_KEY = "reset_identifier"
SENT_MESSAGE = (
    "If an account matches, we've sent a reset link and/or code to its registered contacts."
)
LINK_SENT_MESSAGE = (
    "If an account matches, we've sent a password reset link to its registered email."
)
BAD_CODE = "That code is wrong or has expired."

id_limit = RateLimit("reset:id")
# Per IP like login and reset-verify: a school's pupils often share one NAT address.
ip_limit = RateLimit("reset:ip", limit=50, window=900)
verify_user_limit = RateLimit("reset_verify", limit=5, window=900)
verify_ip_limit = RateLimit("reset_verify_ip", limit=50, window=900)


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

    # Exactly one task for every request that passes the limits, so response time does not
    # reveal whether the account exists. argsrepr keeps the identifier out of Celery logs.
    send_password_reset.apply_async(
        args=[ident, request.build_absolute_uri("/"), ip, get_request_id()],
        argsrepr="('<redacted>',)",
        kwargsrepr="{}",
    )
    if not sms_configured():
        # No code to enter: tell everyone to check email, on the login page.
        messages.info(request, LINK_SENT_MESSAGE)
        return redirect(reverse("accounts:login"))
    request.session[SESSION_KEY] = ident
    messages.info(request, SENT_MESSAGE)
    return redirect(reverse("accounts:reset_verify"))


def _too_many(request, form, waits):
    minutes = max(1, math.ceil(max(waits) / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return render(
        request,
        "accounts/reset_verify.html",
        {"form": form, "error": f"Too many attempts. Try again in {minutes} {unit}."},
        status=429,
    )


@never_cache
def reset_verify_view(request):
    ident = request.session.get(SESSION_KEY)
    if not ident:
        return redirect(reverse("accounts:reset"))
    blank = ResetVerifyForm(User())  # same unbound form whether or not the account exists
    if request.method != "POST":
        return render(request, "accounts/reset_verify.html", {"form": blank})

    found = find_user_by_identifier(ident)
    # Reserve first, on every verify POST. Re-issuing a code does not reset these.
    user_key = f"reset_verify:user:{found.pk}" if found else f"reset_verify:ident:{ident}"
    ip = client_ip(request)
    ip_key = f"reset_verify:ip:{ip}" if ip else None
    user_count = verify_user_limit.hit(user_key)
    ip_count = verify_ip_limit.hit(ip_key) if ip_key else 0
    waits = []
    if user_count > verify_user_limit.limit:
        waits.append(verify_user_limit.retry_after(user_key))
    if ip_key and ip_count > verify_ip_limit.limit:
        waits.append(verify_ip_limit.retry_after(ip_key))
    if waits:
        if ip_key and ip_count <= verify_ip_limit.limit:
            verify_ip_limit.release(ip_key)
        if user_count <= verify_user_limit.limit:
            verify_user_limit.release(user_key)
        return _too_many(request, blank, waits)

    # Code first (counts an attempt); password rules only run for the right code, so
    # similarity validation cannot reveal whether the account exists.
    code = (request.POST.get("code") or "").strip()
    if found is None or not code or not check_reset_code(found, code):
        return render(request, "accounts/reset_verify.html", {"form": blank, "error": BAD_CODE})
    form = ResetVerifyForm(found, request.POST)
    if not form.is_valid():
        return render(request, "accounts/reset_verify.html", {"form": form})
    found.must_change_password = False
    form.save()  # set_password + save
    consume_reset_code(found)
    verify_user_limit.reset(user_key)
    request.session.pop(SESSION_KEY, None)
    request.session.pop("mfa_pending", None)
    request.session.cycle_key()
    record("auth.password_reset", request=request, actor=found, obj=found)
    # A locked-out user must be able to use the new password straight away.
    forget_login_lockout(ident, client_ip(request))
    messages.success(request, "Your password has been changed. Log in with the new password.")
    return redirect(settings.LOGIN_URL)
