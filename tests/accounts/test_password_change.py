import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory
from tests.accounts.helpers import login_verified
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db
NEW_PW = "Brand-new-pass-7731"


def _flagged_student(client):
    with schema_context("school_a"):
        u = StudentProfileFactory().user
        u.must_change_password = True
        u.save()
        client.force_login(u)
    return u


def test_flagged_user_redirected_to_change_page(client_a):
    _flagged_student(client_a)
    r = client_a.get(reverse("home"))
    assert r.status_code == 302 and r["Location"] == reverse("accounts:password_change")
    assert client_a.get(reverse("accounts:password_change")).status_code == 200


def test_exempt_paths(client_a):
    _flagged_student(client_a)
    assert client_a.get("/healthz").status_code == 200
    assert client_a.get(reverse("accounts:reset")).status_code == 200
    assert client_a.post(reverse("accounts:logout")).status_code == 302
    assert client_a.get(reverse("home")).status_code == 302  # logged out: login redirect


def test_api_gets_403_code(client_a):
    _flagged_student(client_a)
    r = client_a.get("/api/v1/me")
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "PASSWORD_CHANGE_REQUIRED" and body["details"] == []


def test_change_clears_flag_keeps_session_and_audits(client_a):
    u = _flagged_student(client_a)
    r = client_a.post(
        reverse("accounts:password_change"),
        {"old_password": DEFAULT_PASSWORD, "new_password1": NEW_PW, "new_password2": NEW_PW},
    )
    assert r.status_code == 302
    assert client_a.get(reverse("home")).status_code == 200
    with schema_context("school_a"):
        u.refresh_from_db()
        assert not u.must_change_password and u.check_password(NEW_PW)
        ev = AuditEvent.objects.get(action="auth.password_changed", object_id=u.pk)
        assert NEW_PW not in str(ev.__dict__)


def test_validators_and_wrong_old_and_same_password(client_a):
    _flagged_student(client_a)
    url = reverse("accounts:password_change")
    for data in (
        {"old_password": "nope", "new_password1": NEW_PW, "new_password2": NEW_PW},
        {"old_password": DEFAULT_PASSWORD, "new_password1": "short", "new_password2": "short"},
        {
            "old_password": DEFAULT_PASSWORD,
            "new_password1": DEFAULT_PASSWORD,
            "new_password2": DEFAULT_PASSWORD,
        },
    ):
        assert client_a.post(url, data).status_code == 200
    assert client_a.get(reverse("home")).status_code == 302


def test_requires_login(client_a):
    r = client_a.get(reverse("accounts:password_change"))
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]


def test_flagged_staff_admin_is_forced_after_mfa(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        u.must_change_password = True
        u.save()
        client_a.force_login(u)
    assert client_a.get(reverse("home")).status_code == 302
    assert client_a.get(reverse("home"))["Location"] == reverse("accounts:password_change")


def test_flagged_mfa_staff_unverified_is_logged_out_not_forced(client_a):
    with schema_context("school_a"):
        from apps.accounts.roles import set_roles

        u = StaffFactory()
        set_roles(u, ["school_admin"], by=None)
        u.must_change_password = True
        u.save()
        client_a.force_login(u)
    r = client_a.get(reverse("home"))
    assert "/accounts/login/" in r["Location"]  # MFA middleware ran first


def test_verified_admin_forced(client_a):
    with schema_context("school_a"):
        from apps.accounts.roles import set_roles

        u = StaffFactory()
        set_roles(u, ["school_admin"], by=None)
        u.must_change_password = True
        u.save()
    login_verified(client_a, u)
    assert client_a.get(reverse("home"))["Location"] == reverse("accounts:password_change")
