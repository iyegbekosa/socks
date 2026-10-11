"""Tenant-aware Celery base task (§7.2): work runs in the school that enqueued it."""

from celery import Task
from django.db import connection
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.core.request_id import request_id_var, schema_name_var

SCHEMA_HEADER = "schema_name"


class MissingTenantError(RuntimeError):
    """A task was enqueued or received without a school schema."""


class UnknownTenantError(MissingTenantError):
    """The schema named in the task message does not belong to any school."""


class CrossTenantEnqueueError(RuntimeError):
    """Code running inside one school tried to enqueue a task for another school."""


class TenantTask(Task):
    """Injects the enqueuing schema as a message header and runs inside it."""

    def apply_async(self, args=None, kwargs=None, **options):
        headers = dict(options.get("headers") or {})
        schema = connection.schema_name
        explicit = headers.get(SCHEMA_HEADER)
        if explicit:
            # Only public code (e.g. Beat fan-out) may target a school other than its own.
            if schema != get_public_schema_name() and explicit != schema:
                raise CrossTenantEnqueueError(
                    f"{self.name} enqueued from {schema!r} with {SCHEMA_HEADER}={explicit!r}"
                )
        elif schema == get_public_schema_name():
            raise MissingTenantError(
                f"{self.name} enqueued from the public schema without an explicit "
                f"{SCHEMA_HEADER} header"
            )
        else:
            headers[SCHEMA_HEADER] = schema
        options["headers"] = headers
        return super().apply_async(args, kwargs, **options)

    def retry(
        self,
        args=None,
        kwargs=None,
        exc=None,
        throw=True,
        eta=None,
        countdown=None,
        max_retries=None,
        **options,
    ):
        # Carry the schema forward explicitly so the retry never lands in public.
        schema = self._schema_from_request()
        if schema:
            headers = dict(options.get("headers") or {})
            headers.setdefault(SCHEMA_HEADER, schema)
            options["headers"] = headers
        return super().retry(args, kwargs, exc, throw, eta, countdown, max_retries, **options)

    def _schema_from_request(self):
        request = self.request
        schema = getattr(request, SCHEMA_HEADER, None)
        if not schema:
            schema = (getattr(request, "headers", None) or {}).get(SCHEMA_HEADER)
        return schema

    @staticmethod
    def _school_exists(schema):
        # schema_context only sets search_path (schema, public), so a typo would silently
        # read public tables; check the tenant registry (lives in public) first.
        with schema_context(get_public_schema_name()):
            return get_tenant_model().objects.filter(schema_name=schema).exists()

    def __call__(self, *args, **kwargs):
        schema = self._schema_from_request()
        if not schema:
            # Raised, not retried: a message without a school can never succeed.
            raise MissingTenantError(f"{self.name} received without a {SCHEMA_HEADER} header")
        if not self._school_exists(schema):
            raise UnknownTenantError(f"{self.name} targets unknown schema {schema!r}")
        with schema_context(schema):
            # Logs (ContextFilter) and Sentry tags (before_send) read these contextvars.
            tokens = [
                (schema_name_var, schema_name_var.set(schema)),
                (request_id_var, request_id_var.set(self.request.id or "-")),
            ]
            try:
                return super().__call__(*args, **kwargs)
            finally:
                for var, token in reversed(tokens):
                    var.reset(token)
