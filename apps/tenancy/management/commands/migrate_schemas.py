"""Project override of ``migrate_schemas`` that skips tenants whose schema does not exist.

Self-service signup creates the School row first and the schema later (in the provisioning
task), so a pending or failed school has no schema yet. Stock django-tenants then raises
``MigrationSchemaMissing`` for *every* tenant. Extension point: the command resolves its
executor through the module-level ``GET_EXECUTOR_FUNCTION`` at run time, and every executor
receives the tenant list in ``run_migrations`` / ``run_multi_type_migrations``. We wrap the
chosen executor class so it filters that list, which works for the standard, multiprocessing
and subprocess executors alike without copying the command's ``handle``.
"""

import logging

from django_tenants.management.commands import migrate_schemas as stock
from django_tenants.utils import get_tenant_database_alias, schema_exists

logger = logging.getLogger(__name__)


def _existing(tenants, database):
    kept = []
    for tenant in tenants or []:
        name = tenant if isinstance(tenant, str) else tenant[0]
        if schema_exists(name, database):
            kept.append(tenant)
        else:
            logger.warning("migrate_schemas: skipping %s, its schema does not exist yet", name)
    return kept


_CACHE: dict[type, type] = {}


def _skipping_executor(codename=None):
    base = stock.get_executor(codename=codename)
    if base in _CACHE:
        return _CACHE[base]

    class SkipMissing(base):  # type: ignore[valid-type, misc]
        def _database(self):
            return self.options.get("database") or get_tenant_database_alias()

        def run_migrations(self, tenants=None):
            return super().run_migrations(tenants=_existing(tenants, self._database()))

        def run_multi_type_migrations(self, tenants):
            return super().run_multi_type_migrations(tenants=_existing(tenants, self._database()))

    SkipMissing.codename = base.codename
    _CACHE[base] = SkipMissing
    return SkipMissing


class Command(stock.MigrateSchemasCommand):
    def handle(self, *args, **options):
        previous = stock.GET_EXECUTOR_FUNCTION
        stock.GET_EXECUTOR_FUNCTION = _skipping_executor
        try:
            super().handle(*args, **options)
        finally:
            stock.GET_EXECUTOR_FUNCTION = previous
