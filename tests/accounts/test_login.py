import logging

import pytest
from django.core.cache import cache
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.core.logging import ContextFilter
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory, StudentFactory

pytestmark = pytest.mark.django_db
GENERIC = "Those details don&#x27;t match an account. Check them and try again."


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _teacher(**kw):
    u = StaffFactory(**kw)
    set_roles(u, ["teacher"], by=u)
    return u


def _post(client, ident, password=DEFAULT_PASSWORD, **extra):
    return client.post(
        reverse("accounts:login"), {"identifier": ident, "password": password, **extra}
    )


def _actions(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action))


def test_login_page_renders(client_a):
    r = client_a.get(reverse("accounts:login"))
    assert r.status_code == 200
    body = r.content.decode()
    assert 'autocomplete="username"' in body and 'autocomplete="current-password"' in body
    assert "csrfmiddlewaretoken" in body and "School A" in body


def test_teacher_login(client_a):
    with schema_context("school_a"):
        u = _teacher()
    r = _post(client_a, u.email)
    assert r.status_code == 302 and r["Location"] == "/"
    assert client_a.session["_auth_user_id"] == str(u.pk)
    assert "last_seen" in client_a.session
    assert len(_actions("auth.login")) >= 1


def test_external_next_ignored(client_a):
    with schema_context("school_a"):
        u = _teacher()
    r = _post(client_a, u.email, next="https://evil.example/")
    assert r["Location"] == "/"


def test_internal_next_followed(client_a):
    with schema_context("school_a"):
        u = _teacher()
    r = _post(client_a, u.email, next="/accounts/logout-ok/")
    assert r["Location"] == "/accounts/logout-ok/"


def test_student_login(client_a):
    with schema_context("school_a"):
        s = StudentFactory()
    r = _post(client_a, s.admission_number.lower())
    assert r.status_code == 302


def test_failures_identical_and_audited(client_a):
    with schema_context("school_a"):
        u = _teacher()
        inactive = _teacher(is_active=False)
    bodies = []
    for ident, pw in [
        (u.email, "wrong-password"),
        ("nobody@school.ng", DEFAULT_PASSWORD),
        (inactive.email, DEFAULT_PASSWORD),
    ]:
        r = _post(client_a, ident, pw)
        assert r.status_code == 200
        assert GENERIC in r.content.decode()
        bodies.append(r.content.decode().count("alert"))
    events = _actions("auth.login_failed")
    assert len(events) == 3
    assert events[1].after["identifier"] == "nobody@school.ng"


def test_lockout_by_identifier(client_a):
    with schema_context("school_a"):
        u = _teacher()
    for _ in range(5):
        assert _post(client_a, u.email, "bad").status_code == 200
    r = _post(client_a, u.email)
    assert r.status_code == 429
    assert "Too many attempts" in r.content.decode() and "15 minutes" in r.content.decode()
    assert "_auth_user_id" not in client_a.session
    assert len(_actions("auth.lockout")) == 1
    _post(client_a, u.email, "bad")
    assert len(_actions("auth.lockout")) == 1


def test_lockout_by_ip(client_a):
    for i in range(5):
        assert _post(client_a, f"nobody{i}@school.ng", "bad").status_code == 200
    with schema_context("school_a"):
        u = _teacher()
    assert _post(client_a, u.email).status_code == 429


def test_success_resets_identifier_key(client_a):
    with schema_context("school_a"):
        u = _teacher()
    for _ in range(3):
        _post(client_a, u.email, "bad")
    assert _post(client_a, u.email).status_code == 302
    client_a.post(reverse("accounts:logout"))
    from apps.accounts.views.login import ip_limit

    ip_limit.reset("login:ip:127.0.0.1")
    for _ in range(4):
        _post(client_a, u.email, "bad")
    assert _post(client_a, u.email).status_code == 302


def test_bursar_gets_mfa_pending_not_login(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["bursar"], by=u)
    r = _post(client_a, u.email)
    assert r.status_code == 302 and r["Location"] == reverse("accounts:mfa")
    assert "_auth_user_id" not in client_a.session
    pending = client_a.session["mfa_pending"]
    assert pending["user_id"] == u.pk and isinstance(pending["at"], int)


def test_logout_get_405_post_logs_out(client_a):
    with schema_context("school_a"):
        u = _teacher()
    _post(client_a, u.email)
    assert client_a.get(reverse("accounts:logout")).status_code == 405
    r = client_a.post(reverse("accounts:logout"))
    assert r.status_code == 302
    assert "_auth_user_id" not in client_a.session
    assert len(_actions("auth.logout")) == 1


def test_home_requires_login(client_a):
    r = client_a.get("/")
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))


@pytest.mark.urls("tests.core.urls_testing")
def test_log_role_staff_and_student(client_a, caplog):
    caplog.handler.addFilter(ContextFilter())
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["bursar", "teacher"], by=u)
        s = StudentFactory()
    for user, expected in ((u, "bursar,teacher"), (s, "student")):
        caplog.clear()
        with schema_context("school_a"):
            client_a.force_login(user)
        with caplog.at_level(logging.WARNING, logger="tests.core"):
            client_a.get("/log")
        records = [r for r in caplog.records if r.name == "tests.core"]
        assert records[0].role == expected
