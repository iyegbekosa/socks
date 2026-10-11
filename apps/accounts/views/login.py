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
ip_limit = RateLimit("login:ip")


def _normalised(raw: str) -> str:
    try:
        return classify_identifier(raw)[1]
    except ValueError:
        return (raw or "").strip().lower()


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

    waits = []
    if id_limit.is_blocked(id_key):
        waits.append(id_limit.retry_after(id_key))
    if ip_key and ip_limit.is_blocked(ip_key):
        waits.append(ip_limit.retry_after(ip_key))
    if waits:
        minutes = max(1, math.ceil(max(waits) / 60))
        return _render(request, form, error=_locked_message(minutes), status=429)

    user = authenticate(request, identifier=raw, password=password)
    if user is None:
        id_count = id_limit.hit(id_key)
        ip_count = ip_limit.hit(ip_key) if ip_key else 0
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
    if user.requires_mfa:
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
