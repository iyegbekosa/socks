"""Background provisioning of a newly signed-up school (platform task, public schema)."""

import logging

from django.conf import settings
from django.db import connection
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.tenancy.models import Domain, School
from apps.tenancy.seeding import seed_school
from config.celery import app

logger = logging.getLogger(__name__)

FIRST_USER_ROLES = ("proprietor", "school_admin")


def _set_status(school_id: int, status: str) -> None:
    # queryset.update(): never goes through TenantMixin.save(), which could create a schema.
    with schema_context(get_public_schema_name()):
        School.objects.filter(pk=school_id).update(provisioning_status=status)


def _invite_contact(school: School, base_url: str) -> None:
    from django.db.models import Q

    from apps.accounts.models import User
    from apps.accounts.services import invite_staff

    query = Q()
    if school.contact_email:
        query |= Q(email__iexact=school.contact_email)
    if school.contact_phone:
        query |= Q(phone=school.contact_phone)
    if User.objects.filter(query, kind=User.Kind.STAFF).exists():
        return  # a previous run already invited them
    invite_staff(
        full_name=school.contact_name,
        email=school.contact_email,
        phone=school.contact_phone,
        roles=FIRST_USER_ROLES,
        by=None,
        base_url=base_url,
    )


@app.task(name="tenancy.provision_school")
def provision_school(school_id: int) -> None:
    """Create the schema, seed it, invite the first admin, add the domain. Safe to re-run."""
    try:
        with schema_context(get_public_schema_name()):
            school = School.objects.get(pk=school_id)
        subdomain = school.schema_name.replace("_", "-")
        base_url = settings.PLATFORM_URL_TEMPLATE.format(subdomain=subdomain)

        school.create_schema(check_if_exists=True, verbosity=0)
        with tenant_context(school):  # a real tenant: invite emails read its name
            seed_school(school)
            _invite_contact(school, base_url)
            from apps.audit.models import AuditEvent
            from apps.audit.services import record

            if not AuditEvent.objects.filter(
                action="school.provisioned", object_id=str(school.pk)
            ).exists():
                record("school.provisioned", obj=school)
        with schema_context(get_public_schema_name()):
            Domain.objects.get_or_create(
                domain=f"{subdomain}.{settings.PLATFORM_DOMAIN}",
                defaults={"tenant": school, "is_primary": True},
            )
        _set_status(school_id, "ready")
    except Exception:
        connection.set_schema_to_public()  # type: ignore[attr-defined]
        logger.exception("School provisioning failed", extra={"school_id": school_id})
        _set_status(school_id, "failed")
        raise
