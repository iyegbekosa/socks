import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db

URL = "/settings/audit/"


def login_as(client, schema, *roles):
    with schema_context(schema):
        u = StaffFactory()
        set_roles(u, list(roles), by=u)
    login_verified(client, u)
    return u


def test_route_name():
    assert reverse("audit:log") == URL


def test_page_one_shows_50_newest(client_a, school_a):
    with schema_context("school_a"):
        AuditEvent.objects.bulk_create(
            [
                AuditEvent(action="bulk.event", object_type="bulk", object_id=str(i))
                for i in range(120)
            ]
        )
    login_as(client_a, "school_a", "school_admin")
    r = client_a.get(URL, {"action": "bulk."})
    page = r.context["page_obj"]
    assert page.paginator.num_pages == 3
    ids = [int(e.object_id) for e in page.object_list]
    assert ids == list(range(119, 69, -1))
    assert "?action=bulk.&amp;page=2" in r.content.decode()
    # invalid and out-of-range pages fall back to the last page
    assert client_a.get(URL, {"action": "bulk.", "page": "999"}).context["page_obj"].number == 3
    assert client_a.get(URL, {"action": "bulk.", "page": "x"}).context["page_obj"].number == 1


def test_action_prefix_filter(client_a, school_a):
    with schema_context("school_a"):
        AuditEvent.objects.create(action="auth.login", object_type="zz", object_id="1")
        AuditEvent.objects.create(action="user.invited", object_type="zz", object_id="2")
    login_as(client_a, "school_a", "school_admin")
    r = client_a.get(URL, {"action": "  auth.  "})
    actions = [e.action for e in r.context["page_obj"].object_list]
    assert actions and all(a.startswith("auth.") for a in actions)
    assert "user.invited" not in r.content.decode()
    long = client_a.get(URL, {"action": "a" * 500})
    assert long.status_code == 200
    assert len(long.context["action"]) == 64


def test_time_renders_in_lagos(client_a, school_a):
    fixed = datetime.datetime(2026, 10, 11, 13, 5, tzinfo=datetime.UTC)
    with schema_context("school_a"), mock.patch("django.utils.timezone.now", return_value=fixed):
        AuditEvent.objects.create(action="time.check", actor_label="", ip=None)
    login_as(client_a, "school_a", "school_admin")
    r = client_a.get(URL, {"action": "time."})
    html = r.content.decode()
    assert "11/10/2026 14:05" in html
    assert "System" in html
    assert "—" in html


def test_before_after_not_rendered(client_a, school_a):
    with schema_context("school_a"):
        AuditEvent.objects.create(
            action="leak.check", before={"phone": "OLDSECRET"}, after={"phone": "NEWSECRET"}
        )
    login_as(client_a, "school_a", "school_admin")
    html = client_a.get(URL, {"action": "leak."}).content.decode()
    assert "leak.check" in html
    assert "OLDSECRET" not in html and "NEWSECRET" not in html


def test_teacher_forbidden(client_a, school_a):
    login_as(client_a, "school_a", "teacher")
    assert client_a.get(URL).status_code == 403


def test_anonymous_redirected(client_a, school_a):
    assert client_a.get(URL).status_code == 302


def test_other_school_events_not_shown(client_a, school_a, school_b):
    with schema_context("school_b"):
        AuditEvent.objects.create(action="only.in.b")
    login_as(client_a, "school_a", "school_admin")
    assert "only.in.b" not in client_a.get(URL).content.decode()
