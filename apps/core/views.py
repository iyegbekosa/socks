from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse


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


def server_error(request, *args, **kwargs) -> JsonResponse:
    """handler500: standard §6.5 body. Django still logs the exception (django.request)."""
    from apps.core.request_id import get_request_id

    body = {
        "error": {
            "code": "SERVER_ERROR",
            "message": "Internal server error",
            "details": [],
            "request_id": get_request_id(),
        }
    }
    return JsonResponse(body, status=500)
