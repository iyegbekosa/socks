from apps.tenancy.models import Domain, School


def make_school(schema_name: str, name: str, domain: str) -> School:
    school, _ = School.objects.get_or_create(schema_name=schema_name, defaults={"name": name})
    Domain.objects.get_or_create(domain=domain, defaults={"tenant": school, "is_primary": True})
    return school
