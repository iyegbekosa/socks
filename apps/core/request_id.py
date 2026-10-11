import re
import uuid
from contextvars import ContextVar

from django.conf import settings
from django.db import connection

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
schema_name_var: ContextVar[str] = ContextVar("schema_name", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")
role_var: ContextVar[str] = ContextVar("role", default="-")


def get_request_id() -> str:
    return request_id_var.get()


def client_ip(request) -> str | None:
    header = getattr(settings, "CLIENT_IP_HEADER", None)
    value = request.META.get(header) if header else None
    if not value:
        value = request.META.get("REMOTE_ADDR")
    if value and "," in value:
        value = value.split(",")[0]
    return value.strip() or None if value else None


def _new_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _accept_or_generate(incoming: str | None) -> str:
    # fullmatch semantics: "$" alone would accept a trailing newline.
    if incoming and _VALID_ID.fullmatch(incoming):
        return incoming
    return _new_id()


class RequestContextMiddleware:
    """Expose request id, schema, user and role to logging; echo ``X-Request-ID``.

    Must sit right after the tenant middleware so the schema is known.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = _accept_or_generate(request.META.get("HTTP_X_REQUEST_ID"))
        request.request_id = request_id
        schema = getattr(getattr(request, "tenant", None), "schema_name", None)
        tokens = [
            (request_id_var, request_id_var.set(request_id)),
            (schema_name_var, schema_name_var.set(schema or connection.schema_name or "-")),
            (user_id_var, user_id_var.set("-")),
            (role_var, role_var.set("-")),
        ]
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    def process_view(self, request, view_func, view_args, view_kwargs):
        # AuthenticationMiddleware has run by now, so request.user is available.
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            user_id_var.set(str(user.pk))
            role_var.set(str(getattr(user, "role", "-")))
