from unittest import mock

import pytest
from django.core import mail, signing
from django.db import connection
from django_tenants.utils import schema_context, tenant_context

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
    login = client.get("/accounts/login/", HTTP_HOST="newschool.localhost")
    assert login.status_code == 200
    assert b"New School" in login.content


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


@pytest.fixture(scope="module", autouse=True)
def _no_leftover_schemas(django_db_blocker):
    """After the module, none of the schemas these tests create may remain (rollback worked)."""
    yield
    with django_db_blocker.unblock():
        assert not {"newschool", "pendingschool"} & schemas()


def test_school_a_collision_variants(client, post, payload, school_a):
    payload["subdomain"] = "School-A"  # lowercases onto school_a's schema and domain
    resp = post(client, payload)
    assert "taken" in str(resp.context["form"].errors["subdomain"])
    payload["subdomain"] = "school_a"  # the regex rejects underscores
    assert "subdomain" in post(client, payload).context["form"].errors
    assert not School.objects.filter(name="New School").exists()


def test_delay_failure_marks_failed(client, post, payload):
    with mock.patch.object(provision_school, "delay", side_effect=RuntimeError("queue down")):
        resp = post(client, payload)
    assert resp.status_code == 302
    assert School.objects.get(schema_name="newschool").provisioning_status == "failed"
    assert b"contact you" in client.get(resp["Location"], HTTP_HOST=PUBLIC_HOST).content


def test_global_cap_returns_429(client, post, payload):
    from apps.tenancy.views import GLOBAL_KEY, _global_limit

    for _ in range(50):
        _global_limit.hit(GLOBAL_KEY)
    resp = post(client, payload)
    assert resp.status_code == 429
    assert "lot of signups" in resp.content.decode()
    assert not School.objects.filter(schema_name="newschool").exists()


def test_signup_sends_no_sms(client, post, payload, settings, sms_outbox):
    settings.SMS_BACKEND = "apps.core.sms.LocmemSmsBackend"
    post(client, payload)
    assert sms_outbox == []
    assert len(mail.outbox) == 1
    with schema_context("newschool"):
        from apps.accounts.models import User

        assert User.objects.get(email="ada@example.com").phone == "+2348031234567"


def test_retry_after_partial_failure_sends_one_invite(client, post, payload):
    """Production shape: on_commit hooks run after invite_staff returns, i.e. with the user row
    already committed, and a failing send fails the task."""
    import apps.accounts.services as svc
    from apps.accounts.models import User

    post_without_mail = payload  # first run goes through the view, with mail broken below
    pending: list = []

    def run_pending(fn):
        def wrapper(*a, **kw):
            result = fn(*a, **kw)
            todo = list(pending)
            pending.clear()
            for hook in todo:
                hook()
            return result

        return wrapper

    with (
        mock.patch.object(svc.transaction, "on_commit", lambda fn: pending.append(fn)),
        mock.patch.object(svc, "invite_staff", run_pending(svc.invite_staff)),
        mock.patch.object(svc, "send_mail", side_effect=RuntimeError("smtp down")),
    ):
        post(client, post_without_mail)
    school = School.objects.get(schema_name="newschool")
    assert school.provisioning_status == "failed"
    assert mail.outbox == []
    with schema_context("newschool"):
        user = User.objects.get(email="ada@example.com")
        assert not user.has_usable_password()

    with (
        mock.patch.object(svc.transaction, "on_commit", lambda fn: pending.append(fn)),
        mock.patch.object(svc, "resend_invite", run_pending(svc.resend_invite)),
    ):
        provision_school(school.pk)
    school.refresh_from_db()
    assert school.provisioning_status == "ready"
    assert len(mail.outbox) == 1
    assert Domain.objects.filter(domain="newschool.localhost").count() == 1


@pytest.fixture
def transaction_capture(django_capture_on_commit_callbacks):
    return lambda _c: django_capture_on_commit_callbacks(execute=True)


def test_migrate_schemas_skips_pending_school(db, caplog):
    from django.core.management import call_command

    school = School(schema_name="pendingschool", name="Pending", provisioning_status="pending")
    school.auto_create_schema = False
    school.save()
    assert "pendingschool" not in schemas()
    call_command("migrate_schemas", tenant=True, interactive=False, verbosity=0)
    assert "pendingschool" not in schemas()
    assert "skipping pendingschool" in caplog.text


def test_rerun_resends_invite_to_unaccepted_contact(client, post, payload, transaction_capture):
    from apps.tenancy.provisioning import _invite_contact

    post(client, payload)
    assert len(mail.outbox) == 1
    school = School.objects.get(schema_name="newschool")
    with tenant_context(school), transaction_capture(client):
        _invite_contact(school, "http://newschool.localhost:8000", resend=True)
    assert len(mail.outbox) == 2
    assert "/accounts/invite/" in mail.outbox[1].body


def test_junk_posts_do_not_consume_global_cap(client, post, payload):
    from apps.tenancy.views import GLOBAL_KEY, _global_limit

    bad = dict(payload, subdomain="9")
    for i in range(51):
        resp = client.post("/signup/", bad, HTTP_HOST=PUBLIC_HOST, REMOTE_ADDR=f"10.0.{i}.1")
        assert resp.status_code == 200
    assert _global_limit.count(GLOBAL_KEY) == 0
    assert post(client, payload).status_code == 302
    assert School.objects.get(schema_name="newschool").provisioning_status == "ready"


def test_migrate_alias_skips_ghost_school(db):
    from django.core.management import call_command

    school = School(schema_name="pendingschool", name="Ghost", provisioning_status="pending")
    school.auto_create_schema = False
    school.save()
    call_command("migrate", tenant=True, interactive=False, verbosity=0)
    assert "pendingschool" not in schemas()
