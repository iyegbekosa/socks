from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions
from rest_framework.response import Response

from apps.core.request_id import get_request_id


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: list[dict] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or []


_CODES: list[tuple[type[Exception], str]] = [
    (exceptions.ValidationError, "VALIDATION_ERROR"),
    (exceptions.NotAuthenticated, "NOT_AUTHENTICATED"),
    (exceptions.AuthenticationFailed, "NOT_AUTHENTICATED"),
    (exceptions.PermissionDenied, "FORBIDDEN"),
    (exceptions.NotFound, "NOT_FOUND"),
    (exceptions.Throttled, "RATE_LIMITED"),
]


def _body(code: str, message: str, details: list[dict]) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": get_request_id(),
        }
    }


def _flatten(detail, prefix: str = "") -> list[dict]:
    """Turn DRF's nested validation detail into [{"field", "message"}, ...]."""
    if isinstance(detail, dict):
        out: list[dict] = []
        for key, value in detail.items():
            out.extend(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(detail, list):
        out = []
        for index, item in enumerate(detail):
            if isinstance(item, dict | list):
                out.extend(_flatten(item, f"{prefix}.{index}" if prefix else str(index)))
            else:
                out.append({"field": prefix or "non_field_errors", "message": str(item)})
        return out
    return [{"field": prefix or "non_field_errors", "message": str(detail)}]


def api_exception_handler(exc, context):
    if isinstance(exc, ApiError):
        return Response(_body(exc.code, exc.message, exc.details), status=exc.status)
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    if not isinstance(exc, exceptions.APIException):
        return None  # unhandled: let Django produce a 500 (and Sentry see it)

    code = next((c for cls, c in _CODES if isinstance(exc, cls)), None)
    headers = {}
    if isinstance(exc, exceptions.ValidationError):
        details = _flatten(exc.detail)
        message = "Some fields are invalid."
    else:
        details = []
        message = str(exc.detail)
        if code is None:
            code = str(exc.default_code).upper()
    if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
        headers["Retry-After"] = str(int(exc.wait))
    if isinstance(exc, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        auth_header = getattr(exc, "auth_header", None)
        if auth_header:
            headers["WWW-Authenticate"] = auth_header
    status = exc.status_code
    if isinstance(exc, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        # DRF downgrades these to 403 when the first authenticator has no
        # WWW-Authenticate header (SessionAuthentication). §6.5 says 401.
        status = 401
    return Response(_body(code, message, details), status=status, headers=headers)
