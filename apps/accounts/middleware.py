import time

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from apps.audit.services import record
from apps.core.request_id import get_request_id

LAST_SEEN_WRITE_INTERVAL = 60


class StaffIdleTimeoutMiddleware:
    """Log staff out after ``STAFF_IDLE_TIMEOUT_SECONDS`` of inactivity. Students are exempt."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not user.is_student:
            now = int(time.time())
            last_seen = request.session.get("last_seen")
            if last_seen is not None and now - int(last_seen) > settings.STAFF_IDLE_TIMEOUT_SECONDS:
                record(
                    "auth.logout",
                    request=request,
                    actor=user,
                    obj=user,
                    after={"reason": "idle_timeout"},
                )
                next_url = request.get_full_path()
                logout(request)
                if request.path.startswith("/api/"):
                    return JsonResponse(
                        {
                            "error": {
                                "code": "NOT_AUTHENTICATED",
                                "message": "Your session expired. Please log in again.",
                                "details": [],
                                "request_id": get_request_id(),
                            }
                        },
                        status=401,
                    )
                return redirect_to_login(next_url, str(settings.LOGIN_URL))
            if last_seen is None or now - int(last_seen) >= LAST_SEEN_WRITE_INTERVAL:
                request.session["last_seen"] = now
        return self.get_response(request)


class MfaEnforcementMiddleware:
    """Log out MFA-required users whose session was not OTP-verified.

    Covers roles granted mid-session. Must run after ``OTPMiddleware``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _exempt(self, path: str) -> bool:
        static = str(settings.STATIC_URL or "/static/")
        if path.startswith(static):
            return True
        return path in {
            reverse("accounts:login"),
            reverse("accounts:logout"),
            reverse("accounts:mfa"),
            reverse("accounts:mfa_setup"),
        }

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and user.requires_mfa
            and not user.is_verified()
            and not self._exempt(request.path)
        ):
            record(
                "auth.logout",
                request=request,
                actor=user,
                obj=user,
                after={"reason": "mfa_required"},
            )
            next_url = request.get_full_path()
            logout(request)
            if request.path.startswith("/api/"):
                return JsonResponse(
                    {
                        "error": {
                            "code": "NOT_AUTHENTICATED",
                            "message": "Two-factor verification is required. Please log in again.",
                            "details": [],
                            "request_id": get_request_id(),
                        }
                    },
                    status=401,
                )
            return redirect_to_login(next_url, str(settings.LOGIN_URL))
        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    """Send users with ``must_change_password`` to the change page until they have changed it.

    Runs after ``MfaEnforcementMiddleware`` so an unverified MFA session is logged out
    first; staff flagged for a change are forced too. API calls get 403
    ``PASSWORD_CHANGE_REQUIRED``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _exempt(self, path: str) -> bool:
        static = str(settings.STATIC_URL or "/static/")
        return (
            path.startswith(static)
            or path.startswith("/accounts/reset/")
            or path
            in {
                reverse("accounts:password_change"),
                reverse("accounts:logout"),
                "/healthz",
            }
        )

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and user.must_change_password
            and not self._exempt(request.path)
        ):
            if request.path.startswith("/api/"):
                return JsonResponse(
                    {
                        "error": {
                            "code": "PASSWORD_CHANGE_REQUIRED",
                            "message": "You must change your password before continuing.",
                            "details": [],
                            "request_id": get_request_id(),
                        }
                    },
                    status=403,
                )
            return redirect(reverse("accounts:password_change"))
        return self.get_response(request)
