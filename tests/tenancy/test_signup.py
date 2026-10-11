from unittest import mock

import pytest
from django.core import mail, signing
from django.db import connection
from django_tenants.utils import schema_context

from apps.tenancy.models import Domain, Plan, School
from apps.tenancy.provisioning import provision_school

PUBLIC_HOST = "localhost"


def schemas() -> set[str]:
    with connection.cursor() as cur:
        cur.execute("select schema_name from information_schema.schemata")
        return {r[0] for r in cur.fetchall()}


@pytest.fixture(autouse=True)
def _public_and_clean(settings, db):
    from django.core.cache import cache

    cache.clear()
    settings.ALLOWED_HOSTS = ["*"]
    # Under the test transaction on-commit hooks run after the tenant context has exited, where
    # a TenantTask (SMS) cannot be enqueued; in production the commit happens inside it.
    settings.SMS_BACKEND = None
    mail.outbox.clear()


@pytest.fixture
def plan(db):
    return Plan.objects.filter(is_active=True).first()


@pytest.fixture
def payload(plan):
    return {
        "school_name": "New School",
        "subdomain": "newschool",
        "levels_offered": ["primary", "jss"],
        "contact_name": "Ada Obi",
        "contact_phone": "0803 123 4567",
        "contact_email": "ada@example.com",
        "plan": plan.code,
        "accept_terms": "on",
    }


@pytest.fixture
def post(client, django_capture_on_commit_callbacks):
    """POST to the signup page, running on-commit hooks (invite emails) as a commit would."""

    def _post(_client, data, **extra):
        with django_capture_on_commit_callbacks(execute=True):
            return client.post("/signup/", data, HTTP_HOST=PUBLIC_HOST, **extra)

    return _post


def test_landing_links_to_signup(client):
    resp = client.get("/", HTTP_HOST=PUBLIC_HOST)
    assert b'href="/signup/"' in resp.content


# Schema creation is real; the schema and rows vanish when pytest-django rolls back the test
# transaction (Postgres DDL is transactional). An explicit DROP SCHEMA inside the transaction
# fails ("pending trigger events"), so tests rely on the rollback.


def test_valid_signup_provisions_school(client, post, payload):
    resp = post(client, payload)
    assert resp.status_code == 302
    school = School.objects.get(schema_name="newschool")
    assert school.provisioning_status == "ready"
    assert school.levels_offered == ["primary", "jss"]
    assert school.contact_phone == "+2348031234567"
    assert Domain.objects.get(domain="newschool.localhost").tenant_id == school.pk

    status = client.get(resp["Location"], HTTP_HOST=PUBLIC_HOST)
    assert status.status_code == 200
    assert b"check your email" in status.content.lower()

    with schema_context("newschool"):
        from django.contrib.auth.models import Group

        from apps.accounts.models import User

        assert Group.objects.filter(name="proprietor").exists()
        user = User.objects.get(email="ada@example.com")
        assert set(user.roles) == {"proprietor", "school_admin"}
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ada@example.com"]
    assert "http://newschool.localhost:8000/" in mail.outbox[0].body
    # The new host routes to the new schema.
    assert client.get("/accounts/login/", HTTP_HOST="newschool.localhost").status_code == 200


@pytest.mark.parametrize(
    "bad",
    ["Admin", "-abc", "abc-", "a", "public", "www", "9lives", "pgadmin", "a--b", "x" * 31],
)
def test_bad_subdomain_rejected(client, post, payload, bad):
    before_schemas, before_count = schemas(), School.objects.count()
    payload["subdomain"] = bad
    resp = post(client, payload)
    assert resp.status_code == 200
    assert "subdomain" in resp.context["form"].errors
    assert School.objects.count() == before_count
    assert schemas() == before_schemas


def test_existing_subdomain_rejected(client, post, payload, school_a):
    before_schemas, before_count = schemas(), School.objects.count()
    payload["subdomain"] = "school-a"  # schema school_a and domain school-a.localhost
    resp = post(client, payload)
    assert "subdomain" in resp.context["form"].errors
    payload["subdomain"] = "SCHOOL_A"
    assert "subdomain" in post(client, payload).context["form"].errors
    assert School.objects.count() == before_count
    assert schemas() == before_schemas


def test_mixed_case_collision_with_domain(db):
    from apps.tenancy.signup import validate_subdomain

    with pytest.raises(Exception, match="taken"):
        validate_subdomain("School-A")


def test_other_field_errors(client, post, payload):
    payload.update(contact_phone="12345", accept_terms="")
    payload.pop("levels_offered")
    resp = post(client, payload)
    errors = resp.context["form"].errors
    assert {"levels_offered", "contact_phone", "accept_terms"} <= set(errors)
    assert not School.objects.filter(schema_name="newschool").exists()


def test_provisioning_failure_marks_failed(client, post, payload):
    with mock.patch("apps.tenancy.provisioning.seed_school", side_effect=RuntimeError("boom")):
        resp = post(client, payload)
    assert resp.status_code == 302  # view swallows the eager exception
    school = School.objects.get(schema_name="newschool")
    assert school.provisioning_status == "failed"
    assert not Domain.objects.filter(domain="newschool.localhost").exists()
    status = client.get(resp["Location"], HTTP_HOST=PUBLIC_HOST)
    assert b"contact you" in status.content


def test_provision_twice_is_idempotent(client, post, payload):
    from django.contrib.auth.models import Group

    from apps.accounts.models import User
    from apps.audit.models import AuditEvent

    post(client, payload)
    school = School.objects.get(schema_name="newschool")
    provision_school(school.pk)
    assert Domain.objects.filter(domain="newschool.localhost").count() == 1
    with schema_context("newschool"):
        assert User.objects.filter(email="ada@example.com").count() == 1
        assert Group.objects.filter(name="proprietor").count() == 1
        assert AuditEvent.objects.filter(action="school.provisioned").count() == 1
    assert len(mail.outbox) == 1
    assert School.objects.get(pk=school.pk).provisioning_status == "ready"


def test_sixth_post_is_rate_limited(client, post, payload):
    payload["subdomain"] = "9"  # invalid: no schema work, still counts
    for _ in range(5):
        assert post(client, payload).status_code == 200
    resp = post(client, payload)
    assert resp.status_code == 429
    assert b"Too many" in resp.content


def test_status_token_bad_or_expired_is_404(client, db):
    assert client.get("/signup/status/garbage/", HTTP_HOST=PUBLIC_HOST).status_code == 404
    token = signing.dumps(1, salt="signup-status")
    with mock.patch(
        "django.core.signing.time.time", return_value=__import__("time").time() + 90000
    ):
        assert client.get(f"/signup/status/{token}/", HTTP_HOST=PUBLIC_HOST).status_code == 404


def test_zz_rolled_back_schemas_are_gone(db):
    """Runs after the provisioning tests: their schemas must not outlive the test transaction."""
    assert "newschool" not in schemas()
