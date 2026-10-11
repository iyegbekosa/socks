import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.models import User
from apps.accounts.services import invite_staff
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory, StudentFactory

pytestmark = pytest.mark.django_db
NEW_PW = "Brand-new-pass-7731"
BASE = "http://school-a.localhost"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _invite(capture, **kw):
    args = {
        "full_name": "Ada Obi",
        "email": "ada@school.ng",
        "phone": "0803 123 4567",
        "roles": ["teacher"],
        "by": None,
        "base_url": BASE,
    }
    args.update(kw)
    with capture(execute=True):
        return invite_staff(**args)


def _link(text):
    return re.search(r"http://school-a\.localhost/accounts/invite/[\w-]+/[\w-]+/", text).group(0)


def test_invite_by_email_and_phone_sends_same_link(
    django_capture_on_commit_callbacks, sms_outbox, school_a
):
    from django.db import connection

    connection.set_tenant(school_a)
    user = _invite(django_capture_on_commit_callbacks)
    assert user.phone == "+2348031234567" and not user.has_usable_password()
    assert user.roles == {"teacher"}
    assert len(mail.outbox) == 1 and len(sms_outbox) == 1
    assert mail.outbox[0].subject == "Your School A staff account"
    assert mail.outbox[0].to == ["ada@school.ng"]
    link = _link(mail.outbox[0].body)
    assert link in sms_outbox[0].body
    assert sms_outbox[0].to == "+2348031234567"
    assert sms_outbox[0].body.startswith(
        "School A: you've been invited to the staff portal. Set your password: "
    )
    with schema_context("school_a"):
        assert AuditEvent.objects.filter(action="user.invited", object_id=user.pk).exists()


def test_no_sms_when_unconfigured(django_capture_on_commit_callbacks, settings, school_a):
    from django.db import connection

    connection.set_tenant(school_a)
    settings.SMS_BACKEND = None
    _invite(django_capture_on_commit_callbacks)
    assert len(mail.outbox) == 1


def test_needs_email_or_phone(django_capture_on_commit_callbacks, school_a):
    from django.db import connection

    connection.set_tenant(school_a)
    with pytest.raises(ValidationError):
        _invite(django_capture_on_commit_callbacks, email="", phone="")


def test_duplicate_email_or_phone_rejected(django_capture_on_commit_callbacks, school_a):
    from django.db import connection

    connection.set_tenant(school_a)
    with schema_context("school_a"):
        StaffFactory(email="dup@school.ng", phone="+2348039999999")
    with pytest.raises(ValidationError):
        _invite(django_capture_on_commit_callbacks, email="DUP@school.ng", phone=None)
    with pytest.raises(ValidationError):
        _invite(django_capture_on_commit_callbacks, email="x@school.ng", phone="08039999999")
    with schema_context("school_a"):
        assert not User.objects.filter(email="x@school.ng").exists()


def _setup_invite(capture, sms_outbox, school_a):
    from django.db import connection

    connection.set_tenant(school_a)
    user = _invite(capture)
    return user, _link(mail.outbox[-1].body)


def test_accept_flow_then_login(client_a, django_capture_on_commit_callbacks, sms_outbox, school_a):
    user, link = _setup_invite(django_capture_on_commit_callbacks, sms_outbox, school_a)
    r = client_a.get(link)
    assert r.status_code == 302
    assert r["Location"].endswith("/set-password/")
    assert link.split("/invite/")[1].split("/")[1] not in r["Location"]
    r = client_a.get(r["Location"])
    assert r.status_code == 200
    r = client_a.post(
        link.replace(link.rstrip("/").rsplit("/", 1)[1], "set-password"),
        {"new_password1": NEW_PW, "new_password2": NEW_PW},
    )
    assert r.status_code == 302 and r["Location"] == reverse("accounts:login")
    assert "_auth_user_id" not in client_a.session
    with schema_context("school_a"):
        assert AuditEvent.objects.filter(action="user.invite_accepted", object_id=user.pk).exists()
    r = client_a.post(
        reverse("accounts:login"), {"identifier": "ada@school.ng", "password": NEW_PW}
    )
    assert r.status_code == 302
    # reuse
    r = client_a.get(link)
    assert r.status_code == 400
    assert b"invalid" in r.content.lower() or b"expired" in r.content.lower()


def test_expired_link(client_a, django_capture_on_commit_callbacks, sms_outbox, school_a):
    user, link = _setup_invite(django_capture_on_commit_callbacks, sms_outbox, school_a)
    import time

    real = time.time()
    with patch("django.contrib.auth.tokens.PasswordResetTokenGenerator._now") as now:
        from datetime import datetime

        now.return_value = datetime.now() + timedelta(hours=73)
        r = client_a.get(link)
    assert real and r.status_code == 400


def test_valid_just_inside_72h(client_a, django_capture_on_commit_callbacks, sms_outbox, school_a):
    user, link = _setup_invite(django_capture_on_commit_callbacks, sms_outbox, school_a)
    with patch("django.contrib.auth.tokens.PasswordResetTokenGenerator._now") as now:
        from datetime import datetime

        now.return_value = datetime.now() + timedelta(hours=71)
        assert client_a.get(link).status_code == 302


def test_garbage_link_400(client_a):
    r = client_a.get(reverse("accounts:invite_accept", args=["zz", "bad-token"]))
    assert r.status_code == 400


def test_student_and_inactive_and_usable_password_are_invalid(client_a, school_a):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    with schema_context("school_a"):
        s = StudentFactory()
        inactive = StaffFactory(is_active=False)
        usable = StaffFactory()
        for u in (s, inactive, usable):
            url = reverse(
                "accounts:invite_accept",
                args=[
                    urlsafe_base64_encode(force_bytes(u.pk)),
                    default_token_generator.make_token(u),
                ],
            )
            assert client_a.get(url).status_code == 400
