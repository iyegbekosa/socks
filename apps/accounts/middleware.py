import time

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

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
