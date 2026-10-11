import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, RequestFactory
from django_tenants.utils import schema_context, tenant_context

from apps.accounts.models import User
from apps.tenancy.middleware import HostTenantMiddleware
from apps.tenancy.models import Domain, School


def _resolve(host: str):
    request = RequestFactory().get("/", HTTP_HOST=host)
    HostTenantMiddleware(lambda r: None).process_request(request)
    return request


@pytest.mark.django_db
def test_school_host_selects_its_schema(school_a):
    assert _resolve("school-a.localhost").tenant.schema_name == "school_a"


@pytest.mark.django_db
def test_custom_user_model_is_active(school_a, school_b):
    assert get_user_model() is User
    with tenant_context(school_a):
        User.objects.create_user(username="only-in-a", password="x", email="a@a.ng")
        assert User.objects.filter(username="only-in-a").exists()
    with tenant_context(school_b):
        assert not User.objects.filter(username="only-in-a").exists()


@pytest.mark.django_db
def test_unknown_host_gets_landing_page(public_tenant):
    response = Client(HTTP_HOST="nosuch.localhost").get("/")

    assert response.status_code == 200
    assert "School Management Platform" in response.content.decode()
    assert response.wsgi_request.tenant.schema_name == "public"


@pytest.mark.django_db
def test_host_with_port_and_capitals_resolves(school_a):
    assert _resolve("School-A.localhost:8000").tenant.schema_name == "school_a"


@pytest.mark.django_db
def test_seed_demo_schools_is_idempotent(public_tenant):
    # Runs inside the test transaction, so the demo schools (and their schemas,
    # since PostgreSQL DDL is transactional) are rolled back afterwards.
    demo_hosts = {"localhost", "greenfield.localhost", "bluewater.localhost"}

    call_command("seed_demo_schools")
    call_command("seed_demo_schools")

    with schema_context("public"):
        domains = Domain.objects.filter(domain__in=demo_hosts)
        assert domains.count() == 3
        assert School.objects.filter(domains__in=domains).distinct().count() == 3
        assert School.objects.filter(schema_name__in=["greenfield", "bluewater"]).count() == 2
