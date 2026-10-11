from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django_tenants.utils import get_public_schema_name

from apps.tenancy.models import Domain, School


class Command(BaseCommand):
    help = "Create the public (platform) tenant and its domain if missing (idempotent)."

    def handle(self, *args, **options):
        connection.set_schema_to_public()
        school, created = School.objects.get_or_create(
            schema_name=get_public_schema_name(), defaults={"name": "Platform"}
        )
        Domain.objects.get_or_create(
            domain=settings.PLATFORM_DOMAIN, defaults={"tenant": school, "is_primary": True}
        )
        self.stdout.write(
            f"{'created' if created else 'exists'}: public ({settings.PLATFORM_DOMAIN})"
        )
