import time

import pytest
from django.core.cache import cache
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.accounts.services import reset_mfa
from apps.audit.models import AuditEvent
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _bursar(with_device=False):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["bursar"], by=u)
        device = None
        if with_device:
            device = TOTPDevice.objects.create(user=u, name="default", confirmed=True)
        return u, device


def _password_login(client, user, **extra):
    return client.post(
        reverse("accounts:login"),
        {"identifier": user.email, "password": DEFAULT_PASSWORD, **extra},
    )


def _actions(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action))


def _set_pending(client, user, age=0):
    with schema_context("school_a"):
        s = client.session
        s["mfa_pending"] = {"user_id": user.pk, "at": int(time.time()) - age}
        s.save()


def _logged_in(client):
    return "_auth_user_id" in client.session


def test_setup_flow_enrols_and_logs_in(client_a):
    user, _ = _bursar()
    r = _password_login(client_a, user)
    assert r["Location"] == reverse("accounts:mfa")
    r = client_a.get(reverse("accounts:mfa"))
    assert r["Location"] == reverse("accounts:mfa_setup")
    page = client_a.get(reverse("accounts:mfa_setup"))
    assert page.status_code == 200
    body = page.content.decode()
    assert "<svg" in body and "School A" not in body.split("<svg")[0] or "<svg" in body
    with schema_context("school_a"):
        device = TOTPDevice.objects.get(user=user)
        assert device.confirmed is False and device.name == "default"
    # Visiting again reuses the same device.
    client_a.get(reverse("accounts:mfa_setup"))
    with schema_context("school_a"):
        assert TOTPDevice.objects.filter(user=user).count() == 1
    import base64

    assert base64.b32encode(device.bin_key).decode().rstrip("=") in body
    r = client_a.post(reverse("accounts:mfa_setup"), {"code": f"{totp(device.bin_key):06d}"})
    assert r.status_code == 302
    assert _logged_in(client_a) and "mfa_pending" not in client_a.session
    with schema_context("school_a"):
        device.refresh_from_db()
        assert device.confirmed is True
    assert len(_actions("auth.mfa_enrolled")) == 1 and len(_actions("auth.login")) == 1
    # The session is OTP-verified: a protected page is reachable (no enforcement logout).
    assert client_a.get("/").status_code == 200
    assert _logged_in(client_a)


def test_otpauth_uses_school_name_and_email(client_a):
    user, _ = _bursar()
    _password_login(client_a, user)
    body = client_a.get(reverse("accounts:mfa_setup")).content.decode()
    # The URI is encoded in the QR; check via the same helper used by the view.
    from apps.accounts.views.mfa import _otpauth_uri

    class Req:
        class tenant:  # noqa: N801
            name = "School A"

    with schema_context("school_a"):
        dev = TOTPDevice.objects.get(user=user)
        uri, _ = _otpauth_uri(Req, user, dev)
    assert uri.startswith("otpauth://totp/School%20A:") and "issuer=School+A" in uri
    assert "svg" in body


def test_verify_flow_with_existing_device(client_a):
    user, device = _bursar(with_device=True)
    r = _password_login(client_a, user)
    assert r["Location"] == reverse("accounts:mfa")
    assert client_a.get(reverse("accounts:mfa")).status_code == 200
    assert client_a.get(reverse("accounts:mfa_setup"))["Location"] == reverse("accounts:mfa")
    r = client_a.post(reverse("accounts:mfa"), {"code": f"{totp(device.bin_key):06d}"})
    assert r.status_code == 302 and _logged_in(client_a)
    assert len(_actions("auth.login")) == 1 and _actions("auth.mfa_enrolled") == []


def test_next_is_honoured_when_safe_only(client_a):
    user, device = _bursar(with_device=True)
    _password_login(client_a, user, next="/somewhere/")
    r = client_a.post(reverse("accounts:mfa"), {"code": f"{totp(device.bin_key):06d}"})
    assert r["Location"] == "/somewhere/"


def test_unsafe_next_ignored(client_a):
    user, device = _bursar(with_device=True)
    _password_login(client_a, user, next="https://evil.example/")
    r = client_a.post(reverse("accounts:mfa"), {"code": f"{totp(device.bin_key):06d}"})
    assert r["Location"] == reverse("home")


def test_five_wrong_codes_clear_pending(client_a):
    user, _ = _bursar(with_device=True)
    _password_login(client_a, user)
    for _ in range(4):
        r = client_a.post(reverse("accounts:mfa"), {"code": "000000"})
        assert r.status_code == 200 and "not correct" in r.content.decode()
    r = client_a.post(reverse("accounts:mfa"), {"code": "000000"})
    assert r["Location"] == reverse("accounts:login")
    assert "mfa_pending" not in client_a.session and not _logged_in(client_a)
    failed = _actions("auth.login_failed")
    assert len(failed) == 5 and all(e.after == {"stage": "mfa"} for e in failed)


def test_lock_survives_new_password_login_and_refuses_correct_code(client_a):
    user, device = _bursar(with_device=True)
    _password_login(client_a, user)
    for _ in range(5):
        client_a.post(reverse("accounts:mfa"), {"code": "000000"})
    r = _password_login(client_a, user)
    assert r["Location"] == reverse("accounts:mfa")
    r = client_a.post(reverse("accounts:mfa"), {"code": f"{totp(device.bin_key):06d}"})
    assert r.status_code == 429 and "Too many attempts" in r.content.decode()
    assert not _logged_in(client_a)


def test_success_resets_counter(client_a):
    user, device = _bursar(with_device=True)
    _password_login(client_a, user)
    for _ in range(3):
        client_a.post(reverse("accounts:mfa"), {"code": "000000"})
    client_a.post(reverse("accounts:mfa"), {"code": f"{totp(device.bin_key):06d}"})
    assert _logged_in(client_a)
    from apps.accounts.views.mfa import mfa_limit

    assert mfa_limit.count(f"mfa:user:{user.pk}") == 0


def test_previous_step_code_accepted(client_a):
    user, device = _bursar(with_device=True)
    _password_login(client_a, user)
    r = client_a.post(reverse("accounts:mfa"), {"code": f"{totp(device.bin_key, drift=-1):06d}"})
    assert r.status_code == 302 and _logged_in(client_a)


def test_reused_code_refused(client_a, school_a):
    from django_tenants.test.client import TenantClient

    user, device = _bursar(with_device=True)
    code = f"{totp(device.bin_key):06d}"
    _password_login(client_a, user)
    assert client_a.post(reverse("accounts:mfa"), {"code": code}).status_code == 302
    other = TenantClient(school_a)
    _password_login(other, user)
    r = other.post(reverse("accounts:mfa"), {"code": code})
    assert r.status_code == 200 and "not correct" in r.content.decode()
    assert "_auth_user_id" not in other.session


def test_pending_expires_after_300s(client_a):
    user, _ = _bursar(with_device=True)
    _set_pending(client_a, user, age=301)
    r = client_a.get(reverse("accounts:mfa"))
    assert r["Location"] == reverse("accounts:login")
    assert "mfa_pending" not in client_a.session


def test_pending_299s_still_valid(client_a):
    user, _ = _bursar(with_device=True)
    _set_pending(client_a, user, age=299)
    assert client_a.get(reverse("accounts:mfa")).status_code == 200


def test_no_pending_redirects_to_login(client_a):
    assert client_a.get(reverse("accounts:mfa"))["Location"] == reverse("accounts:login")
    assert client_a.get(reverse("accounts:mfa_setup"))["Location"] == reverse("accounts:login")


def test_malformed_pending_cleared(client_a):
    with schema_context("school_a"):
        s = client_a.session
        s["mfa_pending"] = {"user_id": "x"}
        s.save()
    assert client_a.get(reverse("accounts:mfa"))["Location"] == reverse("accounts:login")
    assert "mfa_pending" not in client_a.session


def test_pending_for_user_who_lost_mfa_role_cleared(client_a):
    user, _ = _bursar(with_device=True)
    _password_login(client_a, user)
    with schema_context("school_a"):
        set_roles(user, ["teacher"], by=user)
    assert client_a.get(reverse("accounts:mfa"))["Location"] == reverse("accounts:login")
    assert "mfa_pending" not in client_a.session


def test_pending_for_deactivated_user_cleared(client_a):
    user, _ = _bursar(with_device=True)
    _password_login(client_a, user)
    with schema_context("school_a"):
        user.is_active = False
        user.save(update_fields=["is_active"])
    assert client_a.get(reverse("accounts:mfa"))["Location"] == reverse("accounts:login")


def test_role_granted_mid_session_logs_out(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher"], by=u)
        client_a.force_login(u)
    assert client_a.get("/").status_code == 200
    with schema_context("school_a"):
        set_roles(u, ["teacher", "bursar"], by=u)
    r = client_a.get("/")
    assert r.status_code == 302 and r["Location"].startswith(reverse("accounts:login"))
    assert not _logged_in(client_a)
    ev = _actions("auth.logout")[0]
    assert ev.after == {"reason": "mfa_required"}


def test_role_granted_mid_session_api_gets_401(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["bursar"], by=u)
        client_a.force_login(u)
    r = client_a.get("/api/v1/anything")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "NOT_AUTHENTICATED" and "request_id" in err


def test_reset_mfa_removes_devices_and_audits(school_a):
    with schema_context("school_a"):
        user = StaffFactory()
        set_roles(user, ["bursar"], by=user)
        admin = StaffFactory()
        TOTPDevice.objects.create(user=user, name="default", confirmed=True)
        TOTPDevice.objects.create(user=user, name="other", confirmed=False)
        reset_mfa(user, by=admin)
        assert not TOTPDevice.objects.filter(user=user).exists()
        ev = AuditEvent.objects.get(action="auth.mfa_reset")
    assert ev.object_id == str(user.pk) and ev.actor_id == admin.pk
