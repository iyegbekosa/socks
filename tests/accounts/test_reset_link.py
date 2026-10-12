import re
from unittest.mock import patch

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.services import ResetLinkTokenGenerator, reset_link_token_generator
from apps.audit.models import AuditEvent
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db
NEW_PW = "Brand-new-pass-7731"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _request(client, ident):
    return client.post(reverse("accounts:reset"), {"identifier": ident})


def _link(message):
    return re.search(r"https?://\S+/accounts/reset/link/\S+/", message.body).group(0)


def _path(link):
    return "/" + link.split("/", 3)[3]


def _login(client, ident, pw):
    return client.post(reverse("accounts:login"), {"identifier": ident, "password": pw})


def _set(client, link, pw=NEW_PW):
    r = client.get(_path(link))
    assert r.status_code == 302
    return client.post(r["Location"], {"new_password1": pw, "new_password2": pw})


def test_student_link_goes_to_guardian_names_child_and_audits_without_address(client_a):
    with schema_context("school_a"):
        s = StudentProfileFactory(guardian_email="mum@example.com")
        u = s.user
    r = _request(client_a, u.admission_number)
    assert r.status_code == 302
    assert len(mail.outbox) == 1
    m = mail.outbox[0]
    assert m.to == ["mum@example.com"]
    assert m.subject == f"School A: reset the password for {u.first_name}"
    assert u.admission_number in m.body and s.full_name in m.body
    with schema_context("school_a"):
        ev = AuditEvent.objects.get(action="auth.password_reset_link_sent")
        assert "mum@example.com" not in str(ev.__dict__)


def test_falls_back_to_second_guardian_email(client_a):
    with schema_context("school_a"):
        s = StudentProfileFactory(guardian_email="", guardian2_email="dad@example.com")
    _request(client_a, s.user.admission_number)
    assert mail.outbox[0].to == ["dad@example.com"]


def test_siblings_sharing_email_only_reset_the_requested_child(client_a):
    with schema_context("school_a"):
        a = StudentProfileFactory(guardian_email="mum@example.com").user
        b = StudentProfileFactory(guardian_email="mum@example.com").user
    _request(client_a, a.admission_number)
    assert _set(client_a, _link(mail.outbox[0])).status_code == 302
    assert _login(client_a, a.admission_number, NEW_PW).status_code == 302
    client_a.cookies.clear()
    assert _login(client_a, b.admission_number, DEFAULT_PASSWORD).status_code == 302
    assert _login(client_a, b.admission_number, NEW_PW).status_code == 200


def test_staff_link_goes_to_own_email(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
    _request(client_a, u.phone)
    assert mail.outbox[0].to == [u.email]
    assert "admission number" not in mail.outbox[0].body


def test_unknown_and_no_email_are_identical_and_send_nothing(client_a):
    with schema_context("school_a"):
        s = StudentProfileFactory(guardian_email="").user
    pages = []
    for ident in (s.admission_number, "GFS/9999/0001"):
        client_a.cookies.clear()
        cache.clear()
        pages.append(client_a.post(reverse("accounts:reset"), {"identifier": ident}, follow=True))
    assert mail.outbox == []
    strip = lambda p: re.sub(r'value="[^"]*"', "", p.content.decode())  # noqa: E731
    assert strip(pages[0]) == strip(pages[1])
    assert pages[0].redirect_chain == pages[1].redirect_chain


def test_link_works_once_via_tokenless_url_and_never_logs_in(client_a):
    with schema_context("school_a"):
        u = StudentProfileFactory(guardian_email="mum@example.com").user
        u.must_change_password = True
        u.save()
    _request(client_a, u.admission_number)
    link = _link(mail.outbox[0])
    r = client_a.get(_path(link))
    assert r.status_code == 302 and r["Location"] == reverse("accounts:reset_link_set")
    assert r["Referrer-Policy"] == "no-referrer"
    assert link.split("/")[-2] not in r["Location"]
    page = client_a.get(r["Location"])
    assert page.status_code == 200 and page["Referrer-Policy"] == "no-referrer"
    assert "no-store" in page["Cache-Control"]
    r = client_a.post(r["Location"], {"new_password1": NEW_PW, "new_password2": NEW_PW})
    assert r.status_code == 302 and r["Location"] == reverse("accounts:login")
    assert "_auth_user_id" not in client_a.session
    with schema_context("school_a"):
        u.refresh_from_db()
        assert not u.must_change_password
        assert AuditEvent.objects.filter(action="auth.password_reset", object_id=u.pk).exists()
    assert client_a.get(_path(link)).status_code == 400
    assert client_a.get(reverse("accounts:reset_link_set")).status_code == 400


def test_link_fails_after_one_hour(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
    _request(client_a, u.email)
    link = _link(mail.outbox[0])
    now = ResetLinkTokenGenerator()._now()
    from datetime import timedelta

    with patch.object(ResetLinkTokenGenerator, "_now", return_value=now + timedelta(seconds=3601)):
        assert client_a.get(_path(link)).status_code == 400
    assert client_a.get(_path(link)).status_code == 302


def test_invite_and_reset_tokens_do_not_cross(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
    invite = default_token_generator.make_token(u)
    reset = reset_link_token_generator.make_token(u)
    assert reset_link_token_generator.check_token(u, reset)
    assert default_token_generator.check_token(u, invite)
    assert not reset_link_token_generator.check_token(u, invite)
    assert not default_token_generator.check_token(u, reset)


def test_garbage_token_is_400(client_a):
    assert client_a.get("/accounts/reset/link/MQ/bad-token/").status_code == 400


def test_reset_clears_pending_mfa_state(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
    _request(client_a, u.email)
    session = client_a.session
    session["mfa_pending"] = {"user_id": u.pk}
    session.save()
    _set(client_a, _link(mail.outbox[0]))
    assert "mfa_pending" not in client_a.session


def test_sms_code_also_sent_when_configured(client_a, sms_outbox):
    with schema_context("school_a"):
        s = StudentProfileFactory(guardian_email="mum@example.com").user
    r = _request(client_a, s.admission_number)
    assert r["Location"] == reverse("accounts:reset_verify")
    assert len(mail.outbox) == 1 and len(sms_outbox) == 1


def test_every_request_enqueues_exactly_one_task_with_identical_response(client_a):
    from apps.accounts.tasks import send_password_reset

    with schema_context("school_a"):
        s = StudentProfileFactory(guardian_email="mum@example.com").user
    seen = []
    for ident in (s.admission_number, "GFS/9999/0001"):
        client_a.cookies.clear()
        cache.clear()
        with patch.object(send_password_reset, "apply_async") as enqueue:
            r = client_a.post(reverse("accounts:reset"), {"identifier": ident}, follow=True)
        assert enqueue.call_count == 1
        assert enqueue.call_args.kwargs["argsrepr"] == "('<redacted>',)"
        seen.append((r.redirect_chain, re.sub(r'value="[^"]*"', "", r.content.decode())))
    assert seen[0] == seen[1]
    assert mail.outbox == []  # the view itself sends nothing


def test_eager_task_delivers_and_audits_ip(client_a):
    with schema_context("school_a"):
        s = StudentProfileFactory(guardian_email="mum@example.com").user
    _request(client_a, s.admission_number)
    assert len(mail.outbox) == 1
    with schema_context("school_a"):
        ev = AuditEvent.objects.get(action="auth.password_reset_link_sent")
        assert ev.ip and ev.request_id


def test_imported_student_with_unusable_password_can_reset(client_a):
    with schema_context("school_a"):
        u = StudentProfileFactory(guardian_email="mum@example.com").user
        u.set_unusable_password()
        u.must_change_password = True
        u.save()
    _request(client_a, u.admission_number)
    assert len(mail.outbox) == 1
    assert _set(client_a, _link(mail.outbox[0])).status_code == 302
    with schema_context("school_a"):
        u.refresh_from_db()
        assert not u.must_change_password
    assert _login(client_a, u.admission_number, NEW_PW).status_code == 302


def test_pending_staff_invitee_gets_nothing(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        u.set_unusable_password()
        u.save()
    _request(client_a, u.email)
    assert mail.outbox == []


def test_inactive_vs_matching_identical_and_no_email_for_inactive(client_a):
    with schema_context("school_a"):
        dead = StudentProfileFactory(guardian_email="a@example.com", user__is_active=False).user
        live = StudentProfileFactory(guardian_email="b@example.com").user
    pages = []
    for ident in (dead.admission_number, live.admission_number):
        client_a.cookies.clear()
        cache.clear()
        pages.append(client_a.post(reverse("accounts:reset"), {"identifier": ident}, follow=True))
    strip = lambda p: re.sub(r'value="[^"]*"', "", p.content.decode())  # noqa: E731
    assert strip(pages[0]) == strip(pages[1])
    assert [m.to for m in mail.outbox] == [["b@example.com"]]


def test_link_from_school_a_fails_on_school_b(client_a, client_b):
    with schema_context("school_a"):
        u = StaffFactory()
    _request(client_a, u.email)
    assert client_b.get(_path(_link(mail.outbox[0]))).status_code == 400


def test_sms_reset_clears_must_change_password(client_a, sms_outbox):
    with schema_context("school_a"):
        u = StudentProfileFactory().user
        u.must_change_password = True
        u.save()
    _request(client_a, u.admission_number)
    code = re.search(r"code is (\d{6})\.", sms_outbox[-1].body).group(1)
    client_a.post(
        reverse("accounts:reset_verify"),
        {"code": code, "new_password1": NEW_PW, "new_password2": NEW_PW},
    )
    with schema_context("school_a"):
        u.refresh_from_db()
        assert not u.must_change_password
