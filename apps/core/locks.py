"""Per-school PostgreSQL advisory locks."""

from django.db import connection


def school_lock(name: str) -> None:
    """Hold an advisory lock named ``name`` for the current school until the transaction ends.

    The key mixes in ``current_schema()``, so the same feature in two schools never contends.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("school_lock must be called inside transaction.atomic().")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(current_schema() || ':' || %s))", [name]
        )
