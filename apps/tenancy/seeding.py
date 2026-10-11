"""Registry of per-school seeders, run inside the school's schema."""

from collections.abc import Callable

from django_tenants.utils import schema_context

from apps.tenancy.models import School

_SEEDERS: list[Callable[[School], None]] = []


def register_seeder[F: Callable[[School], None]](fn: F) -> F:
    if fn not in _SEEDERS:
        _SEEDERS.append(fn)
    return fn


def seed_school(school: School) -> None:
    """Run every registered seeder in registration order. Seeders must be idempotent."""
    with schema_context(school.schema_name):
        for fn in list(_SEEDERS):
            fn(school)
