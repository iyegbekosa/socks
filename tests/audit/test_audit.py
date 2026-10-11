import pytest
from django.db import InternalError, connection, transaction
from django.test import RequestFactory
from django_tenants.utils import schema_context

from apps.accounts import roles
from apps.audit.models import AuditEvent
from apps.audit.services import record
from apps.core.request_id import client_ip, request_id_var
from tests.accounts.factories import StaffFactory

pytestmark = pytest.mark.django_db


def test_record_stores_actor_ip_request_id(school_a):
    with schema_context("school_a"):
        user = StaffFactory(first_name="Ada", last_name="Obi")
        req = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3")
        req.user = user
        token = request_id_var.set("req_abc")
        try:
            e = record("auth.login", request=req)
        finally:
            request_id_var.reset(token)
        e.refresh_from_db()
        assert (e.actor_id, e.actor_label, e.ip, e.request_id) == (
            user.pk,
            "Ada Obi",
            "10.1.2.3",
            "req_abc",
        )


def test_actor_label_fallbacks_and_anonymous(school_a):
    with schema_context("school_a"):
        u = StaffFactory(first_name="", last_name="", email="x@school.ng")
        assert record("a", actor=u).actor_label == "x@school.ng"
        e = record("a", actor=None)
        assert (e.actor_id, e.actor_label) == (None, "")


def test_obj_and_sensitive_keys_dropped(school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        e = record("x", obj=u, after={"identifier": "x", "password": "p", "otp_code": "1"})
        assert e.after == {"identifier": "x"}
        assert e.object_type == "accounts.user" and e.object_id == str(u.pk)


def test_client_ip_header(settings):
    req = RequestFactory().get("/", REMOTE_ADDR="1.1.1.1", HTTP_CF_CONNECTING_IP="2.2.2.2")
    assert client_ip(req) == "1.1.1.1"
    settings.CLIENT_IP_HEADER = "HTTP_CF_CONNECTING_IP"
    assert client_ip(req) == "2.2.2.2"


def test_append_only_in_db(school_a):
    with schema_context("school_a"):
        e = record("orig")
        attempts = [
            lambda: AuditEvent.objects.filter(pk=e.pk).update(action="x"),
            lambda: AuditEvent.objects.filter(pk=e.pk).delete(),
            lambda: connection.cursor().execute(
                "UPDATE audit_auditevent SET action='x' WHERE id=%s", [e.pk]
            ),
            lambda: connection.cursor().execute("DELETE FROM audit_auditevent WHERE id=%s", [e.pk]),
        ]
        for attempt in attempts:
            with pytest.raises(InternalError), transaction.atomic():
                attempt()
            assert AuditEvent.objects.get(pk=e.pk).action == "orig"


@pytest.mark.parametrize("schema", ["school_a", "school_b"])
def test_trigger_exists_in_each_schema(school_a, school_b, schema):
    with schema_context(schema), connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE t.tgname='audit_event_append_only' AND n.nspname=%s",
            [schema],
        )
        assert cur.fetchone()[0] == 1


def test_set_roles_audited(school_a):
    with schema_context("school_a"):
        admin, user = StaffFactory(), StaffFactory()
        roles.set_roles(user, {"bursar"}, by=admin)
        roles.set_roles(user, {"teacher"}, by=admin)
        evs = list(AuditEvent.objects.filter(action__startswith="role."))
        got = [(e.action, (e.after or e.before)["role"], e.actor_id) for e in evs]
        assert got == [
            ("role.added", "bursar", admin.pk),
            ("role.removed", "bursar", admin.pk),
            ("role.added", "teacher", admin.pk),
        ]


def test_events_isolated_per_schema(school_a, school_b):
    with schema_context("school_a"):
        record("only.a")
    with schema_context("school_b"):
        assert not AuditEvent.objects.filter(action="only.a").exists()


def test_truncate_blocked(school_a):
    with schema_context("school_a"):
        record("keep")
        with pytest.raises(InternalError), transaction.atomic():
            connection.cursor().execute("TRUNCATE audit_auditevent")
        assert AuditEvent.objects.filter(action="keep").exists()


@pytest.mark.parametrize("schema", ["school_a", "school_b"])
def test_truncate_trigger_exists(school_a, school_b, schema):
    with schema_context(schema), connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE t.tgname='audit_event_no_truncate' AND n.nspname=%s",
            [schema],
        )
        assert cur.fetchone()[0] == 1


def test_client_ip_validation(settings):
    rf = RequestFactory()
    assert client_ip(rf.get("/", REMOTE_ADDR="1.1.1.1")) == "1.1.1.1"
    settings.CLIENT_IP_HEADER = "HTTP_X_REAL"
    assert client_ip(rf.get("/", REMOTE_ADDR="1.1.1.1")) == "1.1.1.1"
    multi = rf.get("/", REMOTE_ADDR="1.1.1.1", HTTP_X_REAL="203.0.113.5, 10.0.0.1")
    assert client_ip(multi) == "203.0.113.5"
    assert client_ip(rf.get("/", REMOTE_ADDR="1.1.1.1", HTTP_X_REAL="abc")) == "1.1.1.1"
    assert client_ip(rf.get("/", REMOTE_ADDR="1.1.1.1", HTTP_X_REAL="2001:db8::1")) == "2001:db8::1"
    assert client_ip(rf.get("/", REMOTE_ADDR="junk")) is None


def test_record_with_garbage_ip_header(school_a, settings):
    settings.CLIENT_IP_HEADER = "HTTP_X_REAL"
    with schema_context("school_a"):
        req = RequestFactory().get("/", REMOTE_ADDR="9.9.9.9", HTTP_X_REAL="not-an-ip")
        req.user = StaffFactory()
        assert record("auth.login_failed", request=req).ip == "9.9.9.9"


def test_nested_sensitive_keys_dropped(school_a):
    with schema_context("school_a"):
        e = record(
            "x",
            after={"user": {"password": "x", "name": "Ada"}, "items": [{"otp": "1", "k": "v"}]},
        )
        assert e.after == {"user": {"name": "Ada"}, "items": [{"k": "v"}]}
