"""Student login lockout keys: what resets, releases and clears them (deferred Task 9)."""

import re

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.identifiers import classify_identifier
from apps.accounts.views.login import (
    forget_login_lockout,
    id_ip_key,
    id_ip_limit,
    student_id_limit,
)
from tests.accounts.factories import DEFAULT_PASSWORD
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db
NEW_PW = "Brand-new-pass-7731"
IP = "10.0.7.1"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _ident(admission_number: str) -> str:
    return classify_identifier(admission_number)[1]


def _counts(ident: str, ip: str) -> tuple[int, int]:
    """(all-IP identifier count, identifier+IP count), read in school A's cache namespace."""
    with schema_context("school_a"):
        return student_id_limit.count(f"login:id:{ident}"), id_ip_limit.count(id_ip_key(ident, ip))


def _login(client, ident, pw, ip=IP):
    return client.post(
        reverse("accounts:login"), {"identifier": ident, "password": pw}, REMOTE_ADDR=ip
    )


def _student(**kw):
    with schema_context("school_a"):
        return StudentProfileFactory(**kw).user


def _fail(client, adm, times):
    for _ in range(times):
        assert _login(client, adm, "bad").status_code == 200


def test_forget_login_lockout_without_ip_clears_identifier_key(client_a):
    user = _student()
    ident = _ident(user.admission_number)
    _fail(client_a, user.admission_number, 3)
    assert _counts(ident, IP) == (3, 3)
    with schema_context("school_a"):
        forget_login_lockout(ident, None)  # no crash without an IP
    all_ip, per_ip = _counts(ident, IP)
    assert all_ip == 0
    assert per_ip == 3  # no IP given, so no per-IP key to clear


def test_sixth_refused_attempt_leaves_identifier_count_at_five(client_a):
    user = _student()
    ident = _ident(user.admission_number)
    _fail(client_a, user.admission_number, 5)
    assert _login(client_a, user.admission_number, "bad").status_code == 429
    all_ip, _ = _counts(ident, IP)
    assert all_ip == 5


def test_successful_login_resets_student_identifier_key(client_a):
    user = _student()
    ident = _ident(user.admission_number)
    _fail(client_a, user.admission_number, 4)
    assert _counts(ident, IP) == (4, 4)
    assert _login(client_a, user.admission_number, DEFAULT_PASSWORD).status_code == 302
    assert _counts(ident, IP) == (0, 0)


def test_sms_code_reset_clears_identifier_and_id_ip_keys(client_a, sms_outbox):
    user = _student()
    ident = _ident(user.admission_number)
    _fail(client_a, user.admission_number, 5)
    assert _counts(ident, IP) == (5, 5)
    r = client_a.post(
        reverse("accounts:reset"), {"identifier": user.admission_number}, REMOTE_ADDR=IP
    )
    assert r.status_code == 302
    code = re.search(r"code is (\d{6})\.", sms_outbox[-1].body).group(1)
    r = client_a.post(
        reverse("accounts:reset_verify"),
        {"code": code, "new_password1": NEW_PW, "new_password2": NEW_PW},
        REMOTE_ADDR=IP,
    )
    assert r.status_code == 302 and r["Location"] == reverse("accounts:login")
    assert _counts(ident, IP) == (0, 0)


def test_email_link_reset_clears_identifier_and_id_ip_keys(client_a):
    user = _student(guardian_email="mum@example.com")
    ident = _ident(user.admission_number)
    _fail(client_a, user.admission_number, 5)
    assert _counts(ident, IP) == (5, 5)
    client_a.post(reverse("accounts:reset"), {"identifier": user.admission_number}, REMOTE_ADDR=IP)
    link = re.search(r"https?://\S+/accounts/reset/link/\S+/", mail.outbox[-1].body).group(0)
    r = client_a.get("/" + link.split("/", 3)[3], REMOTE_ADDR=IP)
    assert r.status_code == 302
    r = client_a.post(
        r["Location"], {"new_password1": NEW_PW, "new_password2": NEW_PW}, REMOTE_ADDR=IP
    )
    assert r.status_code == 302
    assert _counts(ident, IP) == (0, 0)
