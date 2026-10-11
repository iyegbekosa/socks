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
