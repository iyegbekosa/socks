"""Give existing schools default assessment components and grading scales (one-time)."""

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.results.models import AssessmentComponent
from apps.results.seeding import seed_results


class Command(BaseCommand):
    help = (
        "Seed default assessment components and grading scales for one school (--schema) or "
        "every school. Only sections without any are filled, so re-running is safe."
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
                seed_results(school)
                count = AssessmentComponent.objects.count()
            self.stdout.write(f"{school.schema_name}: {count} components")
