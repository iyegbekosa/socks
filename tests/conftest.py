import pytest
from django_tenants.utils import get_public_schema_name

from apps.tenancy.models import Domain, School
from tests.helpers import make_school


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Create the public tenant and both test schools once per session."""
    with django_db_blocker.unblock():
        public, _ = School.objects.get_or_create(
            schema_name=get_public_schema_name(), defaults={"name": "Platform"}
        )
        Domain.objects.get_or_create(
            domain="localhost", defaults={"tenant": public, "is_primary": True}
        )
        from apps.tenancy.seeding import seed_school

        for args in (
            ("school_a", "School A", "school-a.localhost"),
            ("school_b", "School B", "school-b.localhost"),
        ):
            seed_school(make_school(*args))
    yield


@pytest.fixture(scope="session")
def public_tenant(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        return School.objects.get(schema_name=get_public_schema_name())


@pytest.fixture(scope="session")
def school_a(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        return School.objects.get(schema_name="school_a")


@pytest.fixture(scope="session")
def school_b(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        return School.objects.get(schema_name="school_b")


@pytest.fixture
def client_a(school_a):
    from django_tenants.test.client import TenantClient

    return TenantClient(school_a)


@pytest.fixture
def client_b(school_b):
    from django_tenants.test.client import TenantClient

    return TenantClient(school_b)


@pytest.fixture(autouse=True)
def _reset_schema_to_public():
    """Middleware under test switches the shared connection; undo it after each test."""
    yield
    from django.db import connection
    from django.urls import set_urlconf

    connection.set_schema_to_public()
    set_urlconf(None)


@pytest.fixture
def sms_outbox(settings):
    """Route SMS to the in-memory outbox and start each test with it empty."""
    from apps.core import sms

    settings.SMS_BACKEND = "apps.core.sms.LocmemSmsBackend"
    sms.outbox.clear()
    yield sms.outbox
    sms.outbox.clear()
