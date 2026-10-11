import re
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory, StudentFactory

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
        s = StudentFactory()
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
        s = StudentFactory()
    _request(client_a, s.admission_number)
    code = _code(sms_outbox)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = _verify(client_a, wrong)
        assert "That code is wrong or has expired." in r.content.decode()
    assert "That code is wrong or has expired." in _verify(client_a, code).content.decode()


def test_expired_code_fails(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentFactory()
    _request(client_a, s.admission_number)
    code = _code(sms_outbox)
    import time

    real = time.time
    with patch("time.time", lambda: real() + 601):
        r = _verify(client_a, code)
    assert "That code is wrong or has expired." in r.content.decode()


def test_new_request_replaces_code(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentFactory()
    _request(client_a, s.admission_number)
    first = _code(sms_outbox)
    _request(client_a, s.admission_number)
    second = _code(sms_outbox)
    if first != second:
        assert "wrong or has expired" in _verify(client_a, first).content.decode()


def test_sixth_request_blocked(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentFactory()
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
        s = StudentFactory()
    for _ in range(5):
        _login(client_a, s.admission_number, "wrong-pass")
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 429
    cache.delete_many([k for k in ()])  # keep limits; only the reset request counters are separate
    _request(client_a, s.admission_number)
    _verify(client_a, _code(sms_outbox))
    assert _login(client_a, s.admission_number, NEW_PW).status_code == 302


def test_login_page_links_to_reset(client_a):
    assert reverse("accounts:reset") in client_a.get(reverse("accounts:login")).content.decode()
