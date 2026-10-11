import re

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_tenants.utils import schema_context

from apps.accounts.models import User
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _login(client, roles):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, roles, by=u)
        client.force_login(u)
    return u


def _admin():
    with schema_context("school_a"):
        a = StaffFactory()
        set_roles(a, ["school_admin"], by=a)
    return a


def _only_admin():
    """Make `a` the only active school_admin in school A."""
    with schema_context("school_a"):
        for u in User.objects.filter(groups__name="school_admin"):
            set_roles(u, [], by=u)
    return _admin()


def _post(client, user, **data):
    return client.post(reverse("staff:edit", args=[user.pk]), data)


def _actions(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action))


def test_list_requires_role(client_a):
    r = client_a.get(reverse("staff:list"))
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))
    _login(client_a, ["teacher"])
    assert client_a.get(reverse("staff:list")).status_code == 403


def test_list_shows_staff_not_students(client_a):
    with schema_context("school_a"):
        st = StudentFactory(first_name="Zed", last_name="Pupil")
        t = StaffFactory(first_name="Tolu", last_name="Teacher")
    _login(client_a, ["proprietor"])
    r = client_a.get(reverse("staff:list"))
    assert r.status_code == 200
    assert b"Tolu" in r.content and b"Zed" not in r.content
    assert st.admission_number.encode() not in r.content
    assert t.get_full_name().encode() in r.content


def test_edit_404_for_student_or_missing(client_a):
    with schema_context("school_a"):
        st = StudentFactory()
    _login(client_a, ["proprietor"])
    assert client_a.get(reverse("staff:edit", args=[st.pk])).status_code == 404
    assert client_a.get(reverse("staff:edit", args=[999999])).status_code == 404


def test_roles_edit_audited(client_a):
    actor = _login(client_a, ["proprietor"])
    with schema_context("school_a"):
        t = StaffFactory()
    r = _post(client_a, t, action="roles", roles=["teacher", "bursar"])
    assert r.status_code == 302
    with schema_context("school_a"):
        assert User.objects.get(pk=t.pk).roles == {"teacher", "bursar"}
    ev = _actions("role.added")
    assert {e.after["role"] for e in ev if e.object_id == str(t.pk)} == {"teacher", "bursar"}
    assert all(e.actor_id == actor.pk for e in ev if e.object_id == str(t.pk))


def test_csrf_enforced_on_post(school_a):
    from django_tenants.test.client import TenantClient

    c = TenantClient(school_a, enforce_csrf_checks=True)
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["proprietor"], by=u)
        c.force_login(u)
        t = StaffFactory()
    assert _post(c, t, action="deactivate").status_code == 403


def test_cannot_remove_last_admin_role(client_a):
    a = _only_admin()
    _login(client_a, ["proprietor"])
    r = _post(client_a, a, action="roles", roles=["teacher"])
    assert r.status_code == 200 and b"last" in r.content.lower()
    with schema_context("school_a"):
        assert "school_admin" in User.objects.get(pk=a.pk).roles


def test_can_remove_admin_role_when_another_exists(client_a):
    a = _only_admin()
    _admin()
    _login(client_a, ["proprietor"])
    assert _post(client_a, a, action="roles", roles=["teacher"]).status_code == 302
    with schema_context("school_a"):
        assert "school_admin" not in User.objects.get(pk=a.pk).roles


def test_cannot_deactivate_last_admin(client_a):
    a = _only_admin()
    _login(client_a, ["proprietor"])
    r = _post(client_a, a, action="deactivate")
    assert r.status_code == 200 and b"last" in r.content.lower()
    with schema_context("school_a"):
        assert User.objects.get(pk=a.pk).is_active


def test_cannot_deactivate_self(client_a):
    me = _login(client_a, ["proprietor"])
    r = _post(client_a, me, action="deactivate")
    assert r.status_code == 200 and b"yourself" in r.content.lower()
    with schema_context("school_a"):
        assert User.objects.get(pk=me.pk).is_active


def test_deactivate_and_reactivate_audited(client_a):
    _login(client_a, ["proprietor"])
    with schema_context("school_a"):
        t = StaffFactory()
    assert _post(client_a, t, action="deactivate").status_code == 302
    with schema_context("school_a"):
        assert not User.objects.get(pk=t.pk).is_active
    assert _post(client_a, t, action="reactivate").status_code == 302
    with schema_context("school_a"):
        assert User.objects.get(pk=t.pk).is_active
    assert [e.object_id for e in _actions("user.deactivated")] == [str(t.pk)]
    assert [e.object_id for e in _actions("user.reactivated")] == [str(t.pk)]


def test_deactivated_mid_session_is_logged_out(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher"], by=u)
        client_a.force_login(u)
    assert client_a.get(reverse("home")).status_code == 200
    with schema_context("school_a"):
        User.objects.filter(pk=u.pk).update(is_active=False)
    r = client_a.get(reverse("home"))
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))


def test_resend_invite(client_a, django_capture_on_commit_callbacks, sms_outbox):
    _login(client_a, ["proprietor"])
    with schema_context("school_a"):
        pending = StaffFactory()
        pending.set_unusable_password()
        pending.save()
        active = StaffFactory()
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(client_a, pending, action="resend")
    assert r.status_code == 302
    assert len(mail.outbox) == 1 and "/accounts/invite/" in mail.outbox[0].body
    assert _actions("user.invited")
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(client_a, active, action="resend")
    assert r.status_code == 200 and len(mail.outbox) == 1


def test_reset_mfa(client_a):
    _login(client_a, ["proprietor"])
    with schema_context("school_a"):
        t = StaffFactory()
        TOTPDevice.objects.create(user=t, name="default", confirmed=True)
    assert _post(client_a, t, action="reset_mfa").status_code == 302
    with schema_context("school_a"):
        assert not TOTPDevice.objects.filter(user=t).exists()
    assert _actions("auth.mfa_reset")


def test_invite_form_creates_staff(client_a, django_capture_on_commit_callbacks):
    _login(client_a, ["proprietor"])
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(
            reverse("staff:invite"),
            {
                "full_name": "New Person",
                "email": "new@school.ng",
                "phone": "",
                "roles": ["teacher"],
            },
        )
    assert r.status_code == 302 and r["Location"] == reverse("staff:list")
    assert len(mail.outbox) == 1
    assert "http://school-a.localhost/accounts/invite/" in mail.outbox[0].body


def test_invite_form_duplicate_shows_field_error(client_a):
    _login(client_a, ["proprietor"])
    with schema_context("school_a"):
        StaffFactory(email="dup2@school.ng")
    r = client_a.post(
        reverse("staff:invite"),
        {"full_name": "X Y", "email": "dup2@school.ng", "phone": "", "roles": ["teacher"]},
    )
    assert r.status_code == 200 and b"already" in r.content.lower()


def test_verified_school_admin_can_manage_staff(client_a):
    with schema_context("school_a"):
        a = StaffFactory()
        set_roles(a, ["school_admin"], by=a)
        t = StaffFactory()
    login_verified(client_a, a)
    assert client_a.get(reverse("staff:list")).status_code == 200
    r = _post(client_a, t, action="roles", roles=["teacher"])
    assert r.status_code == 302
    with schema_context("school_a"):
        assert User.objects.get(pk=t.pk).roles == {"teacher"}


def test_unverified_school_admin_is_sent_to_login(client_a):
    with schema_context("school_a"):
        a = StaffFactory()
        set_roles(a, ["school_admin"], by=a)
        client_a.force_login(a)
    r = client_a.get(reverse("staff:list"))
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))


def test_pending_admin_does_not_count_as_a_second_admin(client_a):
    a = _only_admin()
    with schema_context("school_a"):
        pending = StaffFactory()
        pending.set_unusable_password()
        pending.save()
        set_roles(pending, ["school_admin"], by=a)
    _login(client_a, ["proprietor"])
    r = _post(client_a, a, action="roles", roles=["teacher"])
    assert r.status_code == 200 and b"last" in r.content.lower()
    r = _post(client_a, a, action="deactivate")
    assert r.status_code == 200 and b"last" in r.content.lower()
    with schema_context("school_a"):
        assert User.objects.get(pk=a.pk).is_active
        assert "school_admin" in User.objects.get(pk=a.pk).roles


def test_roles_form_after_rejection_shows_stored_roles(client_a):
    a = _only_admin()
    _login(client_a, ["proprietor"])
    r = _post(client_a, a, action="roles", roles=["teacher"])
    html = r.content.decode()
    assert re.search(r'value="school_admin"[^>]*checked', html)
    assert not re.search(r'value="teacher"[^>]*checked', html)


def test_noop_state_changes_record_nothing(client_a):
    _login(client_a, ["proprietor"])
    with schema_context("school_a"):
        inactive = StaffFactory(is_active=False)
        active = StaffFactory()
    assert _post(client_a, inactive, action="deactivate").status_code == 302
    assert _post(client_a, active, action="reactivate").status_code == 302
    assert not _actions("user.deactivated") and not _actions("user.reactivated")
