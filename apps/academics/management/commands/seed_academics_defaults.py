"""Give schools created before P2 their default class levels and subjects (one-time)."""

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.academics.models import ClassLevel, Subject
from apps.academics.seeding import seed_academics


class Command(BaseCommand):
    help = (
        "Seed default class levels and subjects for one school (--schema) or every school. "
        "Levels are added only where a school has none, subjects likewise, so re-running "
        "is safe and never undoes a school's own changes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--schema", help="Only this school's schema.")

    def handle(self, *args, **options):
        schools = get_tenant_model().objects.exclude(schema_name=get_public_schema_name())
        schema = options.get("schema")
        if schema:
            schools = schools.filter(schema_name=schema)
            if not schools.exists():
                raise CommandError(f"No school with schema {schema!r}.")
        for school in schools.order_by("schema_name"):
            with schema_context(school.schema_name):
                if ClassLevel.objects.exists() and Subject.objects.exists():
                    self.stdout.write(f"{school.schema_name}: skipped (already set up)")
                    continue
                seed_academics(school)
                levels, subjects = ClassLevel.objects.count(), Subject.objects.count()
            self.stdout.write(f"{school.schema_name}: {levels} levels, {subjects} subjects")
