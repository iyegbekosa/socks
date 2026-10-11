import re
import time
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db
NEW_PW = "Brand-new-pass-7731"
GENERIC = "If an account matches, we&#x27;ve sent a code to its registered phone."


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _request(client, ident):
    return client.post(reverse("accounts:reset"), {"identifier": ident})


def _code(outbox):
    return re.search(r"code is (\d{6})\.", outbox[-1].body).group(1)


def _verify(client, code, pw=NEW_PW):
    return client.post(
        reverse("accounts:reset_verify"), {"code": code, "new_password1": pw, "new_password2": pw}
    )


def _login(client, ident, pw):
    return client.post(reverse("accounts:login"), {"identifier": ident, "password": pw})


def test_student_full_flow(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    r = _request(client_a, s.admission_number)
    assert r.status_code == 302 and r["Location"] == reverse("accounts:reset_verify")
    assert len(sms_outbox) == 1 and sms_outbox[0].to == "+2348031234567"
    assert re.fullmatch(
        r"School A: your password reset code is \d{6}\. It expires in 10 minutes.",
        sms_outbox[0].body,
    )
    r = _verify(client_a, _code(sms_outbox))
    assert r.status_code == 302 and r["Location"] == reverse("accounts:login")
    assert "_auth_user_id" not in client_a.session
    assert _login(client_a, s.admission_number, DEFAULT_PASSWORD).status_code == 200
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 302
    with schema_context("school_a"):
        assert AuditEvent.objects.filter(action="auth.password_reset", object_id=s.pk).exists()


def test_staff_uses_own_phone(client_a, sms_outbox):
    with schema_context("school_a"):
        u = StaffFactory()
    _request(client_a, u.email)
    assert sms_outbox[0].to == u.phone


def test_unknown_and_no_phone_identical(client_a, sms_outbox):
    with schema_context("school_a"):
        nophone = StaffFactory(phone=None)
    pages = []
    for ident in ("GFS/9999/0001", nophone.email):
        r = client_a.post(reverse("accounts:reset"), {"identifier": ident}, follow=True)
        pages.append(r.content.decode())
        assert GENERIC in pages[-1]
    assert sms_outbox == []
    strip = lambda p: re.sub(r'value="[^"]*"', "", p)  # noqa: E731
    assert strip(pages[0]) == strip(pages[1])


def test_wrong_five_times_then_right_fails(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    _request(client_a, s.admission_number)
    code = _code(sms_outbox)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = _verify(client_a, wrong)
        assert "That code is wrong or has expired." in r.content.decode()
    r = _verify(client_a, code)
    assert r.status_code != 302 and "_auth_user_id" not in client_a.session
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 200


def test_expired_code_fails(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    _request(client_a, s.admission_number)
    code = _code(sms_outbox)
    real = time.time
    with patch("time.time", lambda: real() + 601):
        r = _verify(client_a, code)
    assert "That code is wrong or has expired." in r.content.decode()


def test_new_request_replaces_code(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    with patch("secrets.randbelow", side_effect=[111111, 222222]):
        _request(client_a, s.admission_number)
        _request(client_a, s.admission_number)
    assert "wrong or has expired" in _verify(client_a, "111111").content.decode()
    assert _verify(client_a, "222222").status_code == 302


def test_reissue_does_not_reset_guess_budget(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    _request(client_a, s.admission_number)
    wrong = "000000" if _code(sms_outbox) != "000000" else "111111"
    for _ in range(5):
        _verify(client_a, wrong)
    _request(client_a, s.admission_number)
    r = _verify(client_a, _code(sms_outbox))
    assert r.status_code == 429
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 200


def test_similar_password_does_not_enumerate(client_a, sms_outbox):
    with schema_context("school_a"):
        u = StaffFactory(email="jane.doe.teacher@school.ng")
    pages = []
    for ident in (u.email, "nobody.here@school.ng"):
        client_a.cookies.clear()
        _request(client_a, ident)
        pages.append(_verify(client_a, "000000", pw=u.email).content.decode())
    csrf = lambda p: re.sub(r'value="[^"]*"', "", p)  # noqa: E731
    assert csrf(pages[0]) == csrf(pages[1])
    assert "wrong or has expired" in pages[0]


def test_password_error_keeps_code_valid(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    _request(client_a, s.admission_number)
    code = _code(sms_outbox)
    r = _verify(client_a, code, pw="short")
    assert r.status_code == 200 and "wrong or has expired" not in r.content.decode()
    assert "_auth_user_id" not in client_a.session
    assert _verify(client_a, code).status_code == 302


def test_success_pops_identifier_and_mfa_pending(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    _request(client_a, s.admission_number)
    session = client_a.session
    session["mfa_pending"] = {"user_id": 1}
    session.save()
    _verify(client_a, _code(sms_outbox))
    assert "reset_identifier" not in client_a.session
    assert "mfa_pending" not in client_a.session


def test_staff_reset_by_phone_identifier(client_a, sms_outbox):
    with schema_context("school_a"):
        u = StaffFactory()
    _request(client_a, u.phone)
    assert sms_outbox[0].to == u.phone
    assert _verify(client_a, _code(sms_outbox)).status_code == 302
    assert _login(client_a, u.email, NEW_PW).status_code == 302


def test_inactive_user_gets_no_sms_and_same_page(client_a, sms_outbox):
    with schema_context("school_a"):
        u = StudentProfileFactory(user__is_active=False).user
    pages = []
    for ident in (u.admission_number, "GFS/9999/0002"):
        client_a.cookies.clear()
        pages.append(client_a.post(reverse("accounts:reset"), {"identifier": ident}, follow=True))
    assert sms_outbox == []
    strip = lambda p: re.sub(r'value="[^"]*"', "", p.content.decode())  # noqa: E731
    assert strip(pages[0]) == strip(pages[1])


def test_has_phone_vs_unknown_identical_request_page(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    a = client_a.post(reverse("accounts:reset"), {"identifier": s.admission_number})
    b = client_a.post(reverse("accounts:reset"), {"identifier": "GFS/9999/0003"})
    assert (a.status_code, a["Location"]) == (b.status_code, b["Location"])
    assert a.content == b.content


def test_sms_unconfigured_is_uniform_and_stores_nothing(client_a, settings):
    settings.DEBUG = False
    settings.SMS_BACKEND = None
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    msg = "Password reset by SMS isn't available yet."
    out = []
    for ident in (s.admission_number, "GFS/9999/0004"):
        r = client_a.post(reverse("accounts:reset"), {"identifier": ident})
        assert r.status_code == 200 and msg in r.content.decode()
        out.append(re.sub(r'value="[^"]*"', "", r.content.decode()))
    assert out[0] == out[1]
    assert client_a.get(reverse("accounts:reset")).status_code == 200
    assert cache.get(f"reset:code:{s.pk}") is None
    assert reverse("accounts:reset") not in client_a.get(reverse("accounts:login")).content.decode()


def test_sixth_request_blocked(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    for _ in range(5):
        assert _request(client_a, s.admission_number).status_code == 302
    assert len(sms_outbox) == 5
    r = _request(client_a, s.admission_number)
    assert r.status_code == 429 and "Too many requests. Try again in" in r.content.decode()
    assert len(sms_outbox) == 5


def test_verify_without_session_redirects(client_a):
    r = client_a.get(reverse("accounts:reset_verify"))
    assert r.status_code == 302 and r["Location"] == reverse("accounts:reset")


def test_reset_clears_login_lockout(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory().user
    for _ in range(5):
        _login(client_a, s.admission_number, "wrong-pass")
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 429
    _request(client_a, s.admission_number)
    _verify(client_a, _code(sms_outbox))
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 302


def test_login_page_links_to_reset(client_a, sms_outbox):
    assert reverse("accounts:reset") in client_a.get(reverse("accounts:login")).content.decode()


def test_reset_request_ip_limit_is_50_per_15_minutes(client_a, sms_outbox):
    from apps.accounts.views.reset import ip_limit

    assert (ip_limit.limit, ip_limit.window) == (50, 900)
    for i in range(50):  # distinct identifiers, so only the per-IP limit can trip
        assert _request(client_a, f"NOPE/{i}").status_code == 302
    r = _request(client_a, "NOPE/last")
    assert r.status_code == 429 and "Too many requests. Try again in" in r.content.decode()


def test_pending_invitee_gets_no_reset_code(client_a, sms_outbox):
    """A staff member who has not accepted their invite (unusable password) must set it via
    the invite link; the reset flow treats them like an unknown account."""
    with schema_context("school_a"):
        pending = StaffFactory()
        pending.set_unusable_password()
        pending.save()
    unknown = client_a.post(reverse("accounts:reset"), {"identifier": "nobody@school.ng"})
    r = _request(client_a, pending.email)
    assert r.status_code == 302 and r["Location"] == reverse("accounts:reset_verify")
    assert r["Location"] == unknown["Location"]
    assert sms_outbox == []
    assert cache.get(f"reset:code:{pending.pk}") is None
    page = client_a.get(reverse("accounts:reset_verify"))
    assert GENERIC in page.content.decode()
