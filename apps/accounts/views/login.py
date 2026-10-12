import math
import time

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.forms import LoginForm
from apps.accounts.identifiers import classify_identifier
from apps.audit.services import record
from apps.core.ratelimit import RateLimit
from apps.core.request_id import client_ip

GENERIC_ERROR = "Those details don't match an account. Check them and try again."

id_limit = RateLimit("login:id")
ip_limit = RateLimit("login:ip", limit=settings.LOGIN_IP_FAILURE_LIMIT)
# Admission numbers are sequential and guessable, so a bad actor could lock any student out
# with five bad passwords. For them the tight (5) limit is per (identifier, IP) and the
# per-identifier limit is looser. Same scope as id_limit so password resets clear both.
id_ip_limit = RateLimit("login:id-ip")
student_id_limit = RateLimit("login:id", limit=settings.STUDENT_IDENTIFIER_FAILURE_LIMIT)


def forget_login_lockout(ident: str, ip: str | None) -> None:
    """Clear the login identifier counters (all-IP key, plus the per-IP key for ``ip``)."""
    id_limit.reset(f"login:id:{ident}")
    pair_key = id_ip_key(ident, ip)
    if pair_key:
        id_ip_limit.reset(pair_key)


def id_ip_key(ident: str, ip: str | None) -> str | None:
    return f"login:id-ip:{ident}:{ip}" if ip else None


def _normalised(raw: str) -> str:
    return classify_identifier(raw)[1]


def _safe_next(request) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return str(settings.LOGIN_REDIRECT_URL)


def _render(request, form, *, error=None, status=200):
    return render(
        request,
        "accounts/login.html",
        {
            "form": form,
            "error": error,
            "next": request.POST.get("next") or request.GET.get("next", ""),
        },
        status=status,
    )


def _locked_message(minutes: int) -> str:
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many attempts. Try again in {minutes} {unit}."


@never_cache
def login_view(request):
    if request.method != "POST":
        if request.user.is_authenticated:
            return redirect(_safe_next(request))
        return _render(request, LoginForm())

    form = LoginForm(request.POST)
    if not form.is_valid():
        return _render(request, form)
    raw = form.cleaned_data["identifier"]
    password = form.cleaned_data["password"]
    ident = _normalised(raw)
    ip = client_ip(request)
    ip_key = f"login:ip:{ip}" if ip else None
    id_key = f"login:id:{ident}"
    is_student = classify_identifier(raw)[0] == "admission"

    # (limiter, key, audit label, is an identifier key), in release/reset order.
    checks: list[tuple[RateLimit, str, str, bool]] = []
    if is_student:
        pair_key = id_ip_key(ident, ip)
        if pair_key:
            checks.append((id_ip_limit, pair_key, "identifier_ip", True))
        checks.append((student_id_limit, id_key, "identifier", True))
    else:
        checks.append((id_limit, id_key, "identifier", True))
    if ip_key:
        checks.append((ip_limit, ip_key, "ip", False))

    # Reserve first: count this attempt on every key before checking the password.
    counts = [limiter.hit(key) for limiter, key, _, _ in checks]
    over = [c > limiter.limit for c, (limiter, _, _, _) in zip(counts, checks, strict=True)]
    if any(over):
        # Refused: give back the hit on every key that was not itself over its limit.
        waits = []
        for is_over, (limiter, key, _, _) in zip(over, checks, strict=True):
            if is_over:
                waits.append(limiter.retry_after(key))
            else:
                limiter.release(key)
        minutes = max(1, math.ceil(max(waits) / 60))
        return _render(request, form, error=_locked_message(minutes), status=429)

    user = authenticate(request, identifier=raw, password=password)
    if user is None:
        record("auth.login_failed", request=request, after={"identifier": ident})
        for c, (limiter, _, by, _) in zip(counts, checks, strict=True):
            if c == limiter.limit:
                record("auth.lockout", request=request, after={"identifier": ident, "by": by})
        return _render(request, form, error=GENERIC_ERROR)

    for limiter, key, _, is_identifier in checks:
        if is_identifier:
            limiter.reset(key)
        else:
            limiter.release(key)
    if user.requires_mfa:
        if request.user.is_authenticated:
            logout(request)
        else:
            request.session.cycle_key()
        request.session["mfa_pending"] = {
            "user_id": user.pk,
            "at": int(time.time()),
            "next": _safe_next(request),
        }
        return redirect(reverse("accounts:mfa"))

    login(request, user)
    request.session["last_seen"] = int(time.time())
    record("auth.login", request=request, actor=user, obj=user)
    return redirect(_safe_next(request))


@require_POST
def logout_view(request):
    user = request.user
    if user.is_authenticated:
        record("auth.logout", request=request, actor=user, obj=user)
    logout(request)
    return redirect(reverse("accounts:login"))
