from django_tenants.utils import schema_context

from apps.results.models import AssessmentComponent, GradeBand
from apps.results.seeding import seed_results


def reseed(school, schema: str = "school_a") -> None:
    with schema_context(schema):
        AssessmentComponent.objects.all().delete()
        GradeBand.objects.all().delete()
        seed_results(school)
