from typing import Any

from apps.audit.models import AuditEvent
from apps.core.logging import is_sensitive_key
from apps.core.request_id import client_ip, get_request_id


def actor_label(user: Any) -> str:
    full_name = user.get_full_name().strip() if hasattr(user, "get_full_name") else ""
    return (
        full_name
        or getattr(user, "email", "")
        or getattr(user, "phone", None)
        or getattr(user, "admission_number", None)
        or ""
    )


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not is_sensitive_key(str(k))}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def _clean(data: dict | None) -> dict | None:
    return None if data is None else _scrub(data)


def record(
    action: str,
    *,
    request: Any = None,
    actor: Any = None,
    obj: Any = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    if actor is None and request is not None:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            actor = user
    if actor is not None and not getattr(actor, "is_authenticated", True):
        actor = None
    object_type = object_id = ""
    if obj is not None:
        object_type = f"{obj._meta.app_label}.{obj._meta.model_name}"
        object_id = str(obj.pk)
    return AuditEvent.objects.create(
        actor_id=actor.pk if actor is not None else None,
        actor_label=actor_label(actor) if actor is not None else "",
        action=action,
        object_type=object_type,
        object_id=object_id,
        before=_clean(before),
        after=_clean(after),
        ip=ip if ip is not None else (client_ip(request) if request is not None else None),
        request_id=request_id
        if request_id is not None
        else (get_request_id() if get_request_id() != "-" else ""),
    )
