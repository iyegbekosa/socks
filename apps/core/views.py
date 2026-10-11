from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.views import defaults


def _check_db() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return "error"
    return "ok"


def _check_cache() -> str:
    try:
        cache.set("healthz", "ok", timeout=5)
        if cache.get("healthz") != "ok":
            return "error"
    except Exception:
        return "error"
    return "ok"


def healthz(request) -> JsonResponse:
    db = _check_db()
    cache_status = _check_cache()
    healthy = db == "ok" and cache_status == "ok"
    body = {"status": "ok" if healthy else "error", "db": db, "cache": cache_status}
    return JsonResponse(body, status=200 if healthy else 503)


def _error_response(code: str, message: str, status: int) -> JsonResponse:
    """Standard §6.5 error body."""
    from apps.core.request_id import get_request_id

    body = {
        "error": {
            "code": code,
            "message": message,
            "details": [],
            "request_id": get_request_id(),
        }
    }
    return JsonResponse(body, status=status)


def _is_api(request) -> bool:
    return request.path.startswith("/api/")


def server_error(request, *args, **kwargs) -> JsonResponse:
    """handler500: standard §6.5 body. Django still logs the exception (django.request)."""
    return _error_response("SERVER_ERROR", "Internal server error", 500)


def not_found(request, exception) -> HttpResponse:
    """handler404: JSON for /api/ paths, Django's HTML page elsewhere."""
    if _is_api(request):
        return _error_response("NOT_FOUND", "Not found.", 404)
    return defaults.page_not_found(request, exception)


def permission_denied(request, exception) -> HttpResponse:
    """handler403: JSON for /api/ paths, Django's HTML page elsewhere."""
    if _is_api(request):
        return _error_response(
            "FORBIDDEN", "You do not have permission to perform this action.", 403
        )
    return defaults.permission_denied(request, exception)


def bad_request(request, exception) -> HttpResponse:
    """handler400: JSON for /api/ paths, Django's HTML page elsewhere."""
    if _is_api(request):
        return _error_response("BAD_REQUEST", "Bad request.", 400)
    return defaults.bad_request(request, exception)
