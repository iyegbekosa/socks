"""`manage.py migrate` is django-tenants' alias of migrate_schemas; keep it the same override."""

from apps.tenancy.management.commands.migrate_schemas import Command

__all__ = ["Command"]
