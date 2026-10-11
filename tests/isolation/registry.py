"""Registry of cross-school isolation cases, and the guard that enforces it.

Every named tenant route under ``api/v1/`` must either have an ``IsolationCase``
(school A must not be able to reach school B's object through it) or be listed
in ``EXEMPT_ROUTES``. Later phases append a case for each route they add.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver

API_PREFIX = "api/v1/"


@dataclass(frozen=True)
class IsolationCase:
    # Full namespaced route name, as passed to reverse() (e.g. "students:list").
    name: str
    method: str
    url: Callable[[Any], str]  # object created in school B -> URL requested as school A
    make_obj: Callable[[], Any]  # creates the object in the active (school B) schema
    body: dict | None = None


def _student_in_active_schema():
    from tests.accounts.factories import StudentFactory

    return StudentFactory.create()


ISOLATION_CASES: list[IsolationCase] = [
    IsolationCase("api:me", "GET", url=lambda u: "/api/v1/me", make_obj=_student_in_active_schema),
]

# Route names deliberately outside the isolation requirement (no tenant data).
EXEMPT_ROUTES: set[str] = {"healthz"}


DRF_METHODS = ("get", "post", "put", "patch", "delete")


def _view_methods(callback) -> list[str]:
    """HTTP methods a view serves (HEAD/OPTIONS excluded).

    Router/ViewSet routes: keys of ``view.actions``. DRF APIViews (``view.cls``) and plain
    class-based views (``view.view_class``): the handlers they define. Function views: GET.
    """
    actions = getattr(callback, "actions", None)
    if actions:
        methods = [m.upper() for m in actions if m.lower() in DRF_METHODS]
    else:
        cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        methods = [m.upper() for m in DRF_METHODS if cls is not None and hasattr(cls, m)]
    return methods or ["GET"]


def _walk(
    patterns, prefix: str = "", namespace: str = ""
) -> Iterator[tuple[str, str | None, list[str]]]:
    """Yield (full route string, namespaced route name or None, methods) per leaf pattern."""
    for entry in patterns:
        # re_path patterns stringify as "^api/v1/...": strip the anchor per segment so
        # regex routes are checked against API_PREFIX like path() routes.
        route = prefix + str(entry.pattern).removeprefix("^")
        if isinstance(entry, URLResolver):
            ns = namespace
            if entry.namespace:
                ns = f"{namespace}:{entry.namespace}" if namespace else entry.namespace
            yield from _walk(entry.url_patterns, route, ns)
        elif isinstance(entry, URLPattern):
            name = entry.name
            if name and namespace:
                name = f"{namespace}:{name}"
            yield route, name, _view_methods(entry.callback)


def unregistered_api_routes(
    urlconf: str = "config.urls_tenant", registered: set[tuple[str, str]] | None = None
) -> list[str]:
    """Return (name, method) pairs under api/v1/ with no case or exemption, as "name METHOD".

    Unnamed routes cannot be registered, so they are reported by route string.
    """
    if registered is None:
        registered = {(c.name, c.method) for c in ISOLATION_CASES}
    registered = {(name, method.upper()) for name, method in registered}
    missing = []
    for route, name, methods in _walk(get_resolver(urlconf).url_patterns):
        if not route.startswith(API_PREFIX):
            continue
        if name is None:
            missing.append(route)
        elif name not in EXEMPT_ROUTES:
            missing.extend(f"{name} {m}" for m in methods if (name, m) not in registered)
    return sorted(set(missing))
