import time

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from tests.accounts.factories import StaffFactory, StudentFactory

pytestmark = pytest.mark.django_db


def _set_last_seen(client, ago):
    with schema_context("school_a"):
        session = client.session
        session["last_seen"] = int(time.time()) - ago
        session.save()


def test_staff_idle_1801_logged_out(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher"], by=u)
    with schema_context("school_a"):
        client_a.force_login(u)
    _set_last_seen(client_a, 1801)
    r = client_a.get("/")
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))
    assert "next=" in r["Location"]
    assert "_auth_user_id" not in client_a.session
    from apps.audit.models import AuditEvent

    with schema_context("school_a"):
        ev = AuditEvent.objects.get(action="auth.logout")
    assert ev.after == {"reason": "idle_timeout"}


def test_staff_1799_passes_and_updates(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher"], by=u)
    with schema_context("school_a"):
        client_a.force_login(u)
    _set_last_seen(client_a, 1799)
    assert client_a.get("/").status_code == 200
    assert time.time() - client_a.session["last_seen"] < 5


def test_student_not_timed_out(client_a):
    with schema_context("school_a"):
        s = StudentFactory()
    with schema_context("school_a"):
        client_a.force_login(s)
    _set_last_seen(client_a, 1801)
    assert client_a.get("/").status_code == 200
