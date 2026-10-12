"""Background provisioning of a newly signed-up school (platform task, public schema)."""

import logging

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django_tenants.postgresql_backend.base import _check_schema_name
from django_tenants.utils import (
    get_public_schema_name,
    schema_context,
    schema_exists,
    tenant_context,
)

from apps.tenancy.models import Domain, School
from apps.tenancy.seeding import seed_school
from config.celery import app

logger = logging.getLogger(__name__)

FIRST_USER_ROLES = ("proprietor", "school_admin")


def _set_status(school_id: int, status: str) -> None:
    # queryset.update(): never goes through TenantMixin.save(), which could create a schema.
    with schema_context(get_public_schema_name()):
        qs = School.objects.filter(pk=school_id)
        if status == "failed":
            qs = qs.exclude(
                provisioning_status="ready"
            )  # a losing concurrent run must not undo success
        qs.update(provisioning_status=status)


def _invite_contact(school: School, base_url: str, *, resend: bool) -> None:
    from django.db.models import Q

    from apps.accounts.models import User
    from apps.accounts.services import invite_staff, resend_invite

    query = Q()
    if school.contact_email:
        query |= Q(email__iexact=school.contact_email)
    if school.contact_phone:
        query |= Q(phone=school.contact_phone)
    existing = User.objects.filter(query, kind=User.Kind.STAFF).first()
    if existing is not None:
        if resend and existing.is_active and not existing.has_usable_password():
            # A previous run created them but the email may never have gone out.
            resend_invite(
                existing,
                by=None,
                base_url=base_url,
                send_sms=False,
                raise_on_send_failure=True,
            )
        return
    invite_staff(
        full_name=school.contact_name,
        email=school.contact_email,
        phone=school.contact_phone,
        roles=FIRST_USER_ROLES,
        by=None,
        base_url=base_url,
        verified=True,  # platform action, not a staff session
        send_sms=False,  # public signup must not be able to trigger SMS to arbitrary numbers
        raise_on_send_failure=True,  # a failed first invite must fail the task so it retries
    )


class DomainNotOwned(Exception):
    """The school's domain row already points at another tenant."""


class SchemaNotOwned(Exception):
    """The school's schema exists but was not created by provisioning: refuse to adopt it."""


def _ensure_own_schema(school: School) -> None:
    """Create the schema, or confirm an existing one is ours from an earlier run.

    CREATE SCHEMA and the ownership flag commit together (Postgres DDL is transactional), so
    the flag is set if and only if this task created the schema. Tables are added afterwards
    by migrate_schemas, so a run that dies mid-migration leaves an owned schema to finish.
    """
    _check_schema_name(school.schema_name)
    with schema_context(get_public_schema_name()), transaction.atomic():
        owned = (
            School.objects.select_for_update()
            .filter(pk=school.pk)
            .values_list("schema_created_by_provisioning", flat=True)
            .get()
        )
        if schema_exists(school.schema_name):
            if not owned:
                raise SchemaNotOwned(
                    f"Schema {school.schema_name!r} already exists and was not created by "
                    "provisioning; refusing to adopt it."
                )
            return
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{school.schema_name}"')
        School.objects.filter(pk=school.pk).update(schema_created_by_provisioning=True)


@app.task(name="tenancy.provision_school")
def provision_school(school_id: int) -> None:
    """Create the schema, seed it, invite the first admin, add the domain. Safe to re-run."""
    try:
        with schema_context(get_public_schema_name()):
            school = School.objects.get(pk=school_id)
        subdomain = school.schema_name.replace("_", "-")
        base_url = settings.PLATFORM_URL_TEMPLATE.format(subdomain=subdomain)

        _ensure_own_schema(school)
        # Create the tables, or finish a schema left half-migrated by an earlier failed run.
        call_command(
            "migrate_schemas",
            tenant=True,
            schema_name=school.schema_name,
            interactive=False,
            verbosity=0,
        )
        with tenant_context(school):  # a real tenant: invite emails read its name
            seed_school(school)
            _invite_contact(school, base_url, resend=school.provisioning_status != "ready")
            from apps.audit.models import AuditEvent
            from apps.audit.services import record

            if not AuditEvent.objects.filter(
                action="school.provisioned", object_id=str(school.pk)
            ).exists():
                record("school.provisioned", obj=school)
        with schema_context(get_public_schema_name()):
            domain, _ = Domain.objects.get_or_create(
                domain=f"{subdomain}.{settings.PLATFORM_DOMAIN}",
                defaults={"tenant": school, "is_primary": True},
            )
        if domain.tenant_id != school.pk:
            raise DomainNotOwned(f"Domain {domain.domain!r} already belongs to another school.")
        _set_status(school_id, "ready")
    except Exception:
        connection.set_schema_to_public()  # type: ignore[attr-defined]
        logger.exception("School provisioning failed", extra={"school_id": school_id})
        _set_status(school_id, "failed")
        raise
