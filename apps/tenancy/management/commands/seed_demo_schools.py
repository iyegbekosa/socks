from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

from apps.tenancy.models import Domain, School

DEMO = [
    ("greenfield", "Greenfield Academy", "greenfield.localhost"),
    ("bluewater", "Bluewater College", "bluewater.localhost"),
]


class Command(BaseCommand):
    help = "Create the public tenant and two demo schools (idempotent)."

    def handle(self, *args, **options):
        call_command("ensure_public_tenant", stdout=self.stdout)
        connection.set_schema_to_public()
        for schema_name, name, domain in DEMO:
            school, created = School.objects.get_or_create(
                schema_name=schema_name, defaults={"name": name}
            )
            Domain.objects.get_or_create(
                domain=domain, defaults={"tenant": school, "is_primary": True}
            )
            self.stdout.write(f"{'created' if created else 'exists'}: {schema_name} ({domain})")
