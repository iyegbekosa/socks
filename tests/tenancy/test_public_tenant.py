import pytest
from django.conf import settings
from django.core.management import call_command

from apps.tenancy.models import Domain, School

pytestmark = pytest.mark.django_db


def test_ensure_public_tenant_is_idempotent(public_tenant):
    call_command("ensure_public_tenant")
    call_command("ensure_public_tenant")
    assert School.objects.filter(schema_name="public").count() == 1
    assert Domain.objects.filter(domain=settings.PLATFORM_DOMAIN).count() == 1
    assert Domain.objects.get(domain=settings.PLATFORM_DOMAIN).tenant.schema_name == "public"
