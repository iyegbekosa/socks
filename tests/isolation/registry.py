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
    name: str  # URL route name this case covers
    method: str
    url: Callable[[Any], str]  # object created in school B -> URL requested as school A
    make_obj: Callable[[], Any]  # creates the object in the active (school B) schema
    body: dict | None = None


ISOLATION_CASES: list[IsolationCase] = []

# Route names deliberately outside the isolation requirement (no tenant data).
EXEMPT_ROUTES: set[str] = {"healthz"}


def _walk(patterns, prefix: str = "") -> Iterator[tuple[str, str | None]]:
    """Yield (full route string, route name or None) for every leaf pattern."""
    for entry in patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _walk(entry.url_patterns, route)
        elif isinstance(entry, URLPattern):
            yield route, entry.name


def unregistered_api_routes(
    urlconf: str = "config.urls_tenant", registered: set[str] | None = None
) -> list[str]:
    """Return named routes under api/v1/ with no case or exemption, plus unnamed ones.

    Unnamed routes cannot be registered, so they are reported by route string.
    """
    if registered is None:
        registered = {c.name for c in ISOLATION_CASES} | EXEMPT_ROUTES
    else:
        registered = registered | EXEMPT_ROUTES
    missing = []
    for route, name in _walk(get_resolver(urlconf).url_patterns):
        if not route.startswith(API_PREFIX):
            continue
        if name is None:
            missing.append(route)
        elif name not in registered:
            missing.append(name)
    return sorted(set(missing))
