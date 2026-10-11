import math
import time

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse
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
    id_key = f"login:id:{ident}"
    ip = client_ip(request)
    ip_key = f"login:ip:{ip}" if ip else None

    # Reserve first: count this attempt on both keys before checking the password.
    id_count = id_limit.hit(id_key)
    ip_count = ip_limit.hit(ip_key) if ip_key else 0
    waits = []
    if id_count > id_limit.limit:
        waits.append(id_limit.retry_after(id_key))
    if ip_key and ip_count > ip_limit.limit:
        waits.append(ip_limit.retry_after(ip_key))
    if waits:
        # Refused: give back the hit on whichever key was not itself over its limit.
        if ip_key and ip_count <= ip_limit.limit:
            ip_limit.release(ip_key)
        if id_count <= id_limit.limit:
            id_limit.release(id_key)
        minutes = max(1, math.ceil(max(waits) / 60))
        return _render(request, form, error=_locked_message(minutes), status=429)

    user = authenticate(request, identifier=raw, password=password)
    if user is None:
        record("auth.login_failed", request=request, after={"identifier": ident})
        if id_count == id_limit.limit or ip_count == ip_limit.limit:
            record(
                "auth.lockout",
                request=request,
                after={
                    "identifier": ident,
                    "by": "identifier" if id_count == id_limit.limit else "ip",
                },
            )
        return _render(request, form, error=GENERIC_ERROR)

    id_limit.reset(id_key)
    if ip_key:
        ip_limit.release(ip_key)
    if user.requires_mfa:
        if request.user.is_authenticated:
            logout(request)
        else:
            request.session.cycle_key()
        request.session["mfa_pending"] = {"user_id": user.pk, "at": int(time.time())}
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


def mfa_placeholder(request):
    """Replaced by the real MFA view in Task 7."""
    return HttpResponse(status=501)
