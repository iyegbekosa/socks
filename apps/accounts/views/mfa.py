import base64
import math
import time
from urllib.parse import quote, urlencode

import segno
from django.conf import settings
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.audit.services import record
from apps.core.ratelimit import RateLimit

mfa_limit = RateLimit("mfa", limit=5, window=900)
# Second, daily cap per user: 5 per 15 minutes alone would still allow ~480 guesses a day.
mfa_daily_limit = RateLimit("mfa_day", limit=20, window=86400)
DEVICE_NAME = "default"
INVALID_CODE = "That code is not correct. Check your authenticator app and try again."


def _login_redirect(request):
    request.session.pop("mfa_pending", None)
    return redirect(reverse("accounts:login"))


def _pending_user(request) -> User | None:
    """Return the user in a valid ``mfa_pending`` entry, clearing it when unusable."""
    pending = request.session.get("mfa_pending")
    if not isinstance(pending, dict):
        return None
    user_id, at = pending.get("user_id"), pending.get("at")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or not isinstance(at, int):
        return None
    if time.time() - at > settings.MFA_PENDING_SECONDS:
        return None
    user = User.objects.filter(pk=user_id, kind="staff", is_active=True).first()
    if user is None or not user.requires_mfa:
        return None
    return user


def _confirmed_device(user: User) -> TOTPDevice | None:
    return TOTPDevice.objects.filter(user=user, confirmed=True).order_by("pk").first()


def _key(user: User) -> str:
    return f"mfa:user:{user.pk}"


def _blocked_message(key: str) -> str:
    waits = [lim.retry_after(key) for lim in (mfa_limit, mfa_daily_limit) if lim.is_blocked(key)]
    minutes = max(1, math.ceil(max(waits, default=0) / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many attempts. Try again in {minutes} {unit}."


def _next_url(request) -> str:
    target = request.session.get("mfa_pending", {}).get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return str(settings.LOGIN_REDIRECT_URL)


def _complete(request, user: User, device: TOTPDevice, *, enrolled: bool):
    target = _next_url(request)
    mfa_limit.reset(_key(user))
    mfa_daily_limit.reset(_key(user))  # the daily cap is for failures; logins are unlimited
    login(request, user)  # rotates the session key
    otp_login(request, device)
    request.session["last_seen"] = int(time.time())
    if enrolled:
        record("auth.mfa_enrolled", request=request, actor=user, obj=user)
    record("auth.login", request=request, actor=user, obj=user)
    request.session.pop("mfa_pending", None)
    return redirect(target)


def _check_code(request, user: User, device: TOTPDevice):
    """Reserve an attempt, then validate the posted code.

    Returns (verified, blocked, error, response).
    """
    key = _key(user)
    # Reserve first in both limiters, so concurrent guesses are counted.
    count = mfa_limit.hit(key)
    day_count = mfa_daily_limit.hit(key)
    if count > mfa_limit.limit or day_count > mfa_daily_limit.limit:
        # Give back the reservation in a limiter that did not refuse this attempt.
        if count <= mfa_limit.limit:
            mfa_limit.release(key)
        if day_count <= mfa_daily_limit.limit:
            mfa_daily_limit.release(key)
        return False, True, _blocked_message(key), None
    token = "".join(request.POST.get("code", "").split())
    if token.isdigit() and device.verify_token(token):
        return True, False, None, None
    record("auth.login_failed", request=request, actor=user, obj=user, after={"stage": "mfa"})
    if count == mfa_limit.limit:
        record("auth.lockout", request=request, actor=user, obj=user, after={"stage": "mfa"})
        return False, False, None, _login_redirect(request)
    return False, False, INVALID_CODE, None


def _setup_device(request, user: User) -> TOTPDevice:
    """The unconfirmed device bound to this pending entry; a fresh one per entry."""
    pending = request.session["mfa_pending"]
    device_id = pending.get("device_id")
    if device_id is not None:
        device = TOTPDevice.objects.filter(pk=device_id, user=user, confirmed=False).first()
        if device is not None:
            return device
    TOTPDevice.objects.filter(user=user, confirmed=False).delete()
    device = TOTPDevice.objects.create(user=user, name=DEVICE_NAME, confirmed=False)
    pending["device_id"] = device.pk
    request.session["mfa_pending"] = pending
    return device


def _otpauth_uri(request, user: User, device: TOTPDevice) -> tuple[str, str]:
    secret = base64.b32encode(device.bin_key).decode().rstrip("=")
    issuer = request.tenant.name
    label = user.email or user.phone or str(user.pk)
    query = urlencode({"secret": secret, "issuer": issuer, "digits": 6, "period": 30})
    return f"otpauth://totp/{quote(issuer)}:{quote(label)}?{query}", secret


@never_cache
def mfa_setup_view(request):
    user = _pending_user(request)
    if user is None:
        return _login_redirect(request)
    if _confirmed_device(user) is not None:
        return redirect(reverse("accounts:mfa"))
    device = _setup_device(request, user)

    error, blocked = None, False
    if request.method == "POST":
        ok, blocked, error, response = _check_code(request, user, device)
        if response is not None:
            return response
        if ok:
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            return _complete(request, user, device, enrolled=True)

    uri, secret = _otpauth_uri(request, user, device)
    qr_svg = segno.make(uri, error="m").svg_inline(scale=5, border=2, title="Authenticator QR code")
    return render(
        request,
        "accounts/mfa_setup.html",
        {"qr_svg": qr_svg, "secret": secret, "error": error},
        status=429 if blocked else 200,
    )


@never_cache
def mfa_verify_view(request):
    user = _pending_user(request)
    if user is None:
        return _login_redirect(request)
    device = _confirmed_device(user)
    if device is None:
        return redirect(reverse("accounts:mfa_setup"))

    error, blocked = None, False
    if request.method == "POST":
        ok, blocked, error, response = _check_code(request, user, device)
        if response is not None:
            return response
        if ok:
            return _complete(request, user, device, enrolled=False)
    return render(
        request,
        "accounts/mfa_verify.html",
        {"error": error},
        status=429 if blocked else 200,
    )
