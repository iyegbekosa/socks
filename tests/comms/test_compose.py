"""P6 Task 4: composing an announcement (``comms:compose``), HTML side."""

import pytest
from django.core import mail
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.comms.models import Message
from tests.accounts.helpers import login_verified
from tests.comms.helpers import DAD, MUM, give_debt, make_world

pytestmark = pytest.mark.django_db

COMPOSE = "/messages/new/"


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = make_world()
        mail.outbox.clear()
        yield world


def _token(client):
    r = client.get(COMPOSE)
    assert r.status_code == 200
    return r.context["form"]["token"].value()


def _data(token, **kw):
    data = {
        "token": token,
        "audience": "school",
        "subject": "Sports day",
        "body": "Sports day at {school_name} is on Friday.",
        "action": "send",
    }
    data.update(kw)
    return data


def _count():
    with schema_context("school_a"):
        return Message.objects.count()


def test_urls():
    assert reverse("comms:compose") == COMPOSE
    assert reverse("comms:history") == "/messages/"
    assert reverse("comms:message", args=[4]) == "/messages/4/"
    assert reverse("comms:retry_failed", args=[4]) == "/messages/4/retry/"
    assert reverse("comms:inbox") == "/inbox/"


def test_admin_previews_then_sends_to_one_arm(client_a, w, django_capture_on_commit_callbacks):
    login_verified(client_a, w.admin)
    token = _token(client_a)
    data = _data(token, audience="arm", arm=w.arm_a.pk, action="preview")
    r = client_a.post(COMPOSE, data)
    assert r.status_code == 200
    preview = r.context["preview"]
    # Ada and Bola share one guardian email; Dayo has none; the withdrawn student is out.
    assert (preview["email_count"], preview["inapp_count"]) == (1, 3)
    assert preview["body"] == "Sports day at School A is on Friday."
    html = r.content.decode()
    assert "Sports day at School A is on Friday." in html
    assert _count() == 0  # a preview writes nothing

    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(COMPOSE, {**data, "action": "send"})
    with schema_context("school_a"):
        (msg,) = Message.objects.all()
        assert r.status_code == 302
        assert r["Location"] == reverse("comms:message", args=[msg.pk])
        assert msg.kind == "announcement"
        assert msg.audience_label == "JSS 1A"
        assert msg.created_by_id == w.admin.pk
        assert msg.deliveries.filter(channel="email").count() == 1
        assert msg.deliveries.filter(channel="inapp").count() == 3
    assert [m.to for m in mail.outbox] == [[MUM]]

    r = client_a.get(reverse("comms:history"))
    html = r.content.decode()
    assert "Sports day" in html and "JSS 1A" in html
    row = r.context["page"].object_list[0]
    assert (row.sent_count, row.queued_count, row.failed_count, row.read_count) == (4, 0, 0, 0)

    r = client_a.get(reverse("comms:message", args=[msg.pk]))
    html = r.content.decode()
    assert "m***@example.com" in html
    assert MUM not in html
    assert "Ada" in html and "JSS 1A" in html


def test_preview_and_send_counts_for_the_whole_school(
    client_a, w, django_capture_on_commit_callbacks
):
    login_verified(client_a, w.principal)
    token = _token(client_a)
    r = client_a.post(COMPOSE, _data(token, action="preview"))
    assert (r.context["preview"]["email_count"], r.context["preview"]["inapp_count"]) == (2, 4)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(COMPOSE, _data(token))
    assert r.status_code == 302
    assert sorted(m.to[0] for m in mail.outbox) == sorted([MUM, DAD])
    with schema_context("school_a"):
        msg = Message.objects.get()
        event = AuditEvent.objects.get(action="comms.message_sent")
        assert event.after == {
            "message_id": msg.pk,
            "kind": "announcement",
            "email_count": 2,
            "inapp_count": 4,
        }


def test_replayed_token_sends_nothing_twice(client_a, w, django_capture_on_commit_callbacks):
    login_verified(client_a, w.admin)
    token = _token(client_a)
    with django_capture_on_commit_callbacks(execute=True):
        first = client_a.post(COMPOSE, _data(token))
        second = client_a.post(COMPOSE, _data(token), follow=True)
    assert first.status_code == 302
    assert second.redirect_chain[-1][0] == reverse("comms:history")
    assert "This message was already sent." in second.content.decode()
    assert _count() == 1


def test_forged_token_is_refused(client_a, w):
    login_verified(client_a, w.admin)
    _token(client_a)
    r = client_a.post(COMPOSE, _data("not-a-token"), follow=True)
    assert r.redirect_chain[-1][0] == COMPOSE
    html = r.content.decode()
    assert "This form has expired or was already used — nothing was sent." in html
    assert "already sent" not in html
    assert _count() == 0


def test_expired_session_token_says_nothing_was_sent(client_a, w):
    login_verified(client_a, w.admin)
    token = _token(client_a)
    session = client_a.session
    session["comms_compose_tokens"] = []
    session.save()
    r = client_a.post(COMPOSE, _data(token), follow=True)
    assert "nothing was sent" in r.content.decode()
    assert _count() == 0


def test_back_then_preview_after_send_does_not_offer_send_again(
    client_a, w, django_capture_on_commit_callbacks
):
    login_verified(client_a, w.admin)
    token = _token(client_a)
    with django_capture_on_commit_callbacks(execute=True):
        assert client_a.post(COMPOSE, _data(token)).status_code == 302
    r = client_a.post(COMPOSE, _data(token, action="preview"), follow=True)
    assert r.redirect_chain[-1][0] == reverse("comms:history")
    assert "This message was already sent." in r.content.decode()
    assert _count() == 1


def test_send_when_message_with_live_token_exists_redirects(client_a, w):
    """The DB ``exists()`` branch: the token is still live but its message was created."""
    from apps.comms import service

    login_verified(client_a, w.admin)
    token = _token(client_a)
    with schema_context("school_a"):
        service.notify(
            "announcement",
            students=[w.a],
            subject="S",
            body="B",
            per_student=False,
            dedupe_key=f"compose:{token}",
        )
    r = client_a.post(COMPOSE, _data(token))
    assert r.status_code == 302 and r["Location"] == reverse("comms:history")
    assert _count() == 1


def test_concurrent_send_integrity_error_redirects(client_a, w):
    """The ``IntegrityError`` branch: another request created the message in between."""
    from unittest import mock

    from django.db import IntegrityError

    login_verified(client_a, w.admin)
    token = _token(client_a)
    with (
        mock.patch("apps.comms.views.compose._sent_with", return_value=False),
        mock.patch("apps.comms.announcements.send", side_effect=IntegrityError),
    ):
        r = client_a.post(COMPOSE, _data(token), follow=True)
    assert r.redirect_chain[-1][0] == reverse("comms:history")
    assert "This message was already sent." in r.content.decode()
    assert _count() == 0


def test_invalid_send_keeps_the_token(client_a, w):
    login_verified(client_a, w.admin)
    token = _token(client_a)
    r = client_a.post(COMPOSE, _data(token, subject=""))
    assert r.status_code == 200
    assert r.context["form"]["token"].value() == token
    r = client_a.post(COMPOSE, _data(token))
    assert r.status_code == 302
    assert _count() == 1


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"body": "Hello {student_first_name}"}, "Unknown placeholder {student_first_name}"),
        ({"body": "Hello {nope}"}, "Unknown placeholder {nope}"),
        ({"body": "Hello {"}, "Unknown placeholder"),
        ({"subject": ""}, "This field is required."),
        ({"subject": "x" * 121}, "at most 120 characters"),
        ({"body": "x" * 2001}, "at most 2000 characters"),
        ({"audience": "arm"}, "Choose a class."),
        ({"audience": "level"}, "Choose a class level."),
        ({"audience": "students", "students": ""}, "Enter at least one admission number."),
    ],
)
def test_form_errors(client_a, w, override, error):
    login_verified(client_a, w.admin)
    r = client_a.post(COMPOSE, _data(_token(client_a), **override))
    assert r.status_code == 200
    assert error in r.content.decode().replace("&#x27;", "'")
    assert _count() == 0


def test_audience_with_no_students(client_a, w):
    login_verified(client_a, w.admin)
    with schema_context("school_a"):
        from apps.academics.models import ClassArm

        empty = ClassArm.objects.create(level=w.arm_a.level, suffix="C")
    for action in ("preview", "send"):
        r = client_a.post(
            COMPOSE, _data(_token(client_a), audience="arm", arm=empty.pk, action=action)
        )
        assert "No students match this audience." in r.content.decode()
    assert _count() == 0


def test_students_by_admission_number(client_a, w, django_capture_on_commit_callbacks):
    login_verified(client_a, w.admin)
    token = _token(client_a)
    numbers = "gfs/2026/0001,\nGFS/2026/0003\n\n"
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(COMPOSE, _data(token, audience="students", students=numbers))
    assert r.status_code == 302
    with schema_context("school_a"):
        msg = Message.objects.get()
        assert msg.audience_label == "2 students"
        assert set(msg.deliveries.values_list("student_id", flat=True)) == {w.a.pk, w.c.pk}


def test_unknown_or_withdrawn_admission_numbers_are_listed(client_a, w):
    login_verified(client_a, w.admin)
    numbers = "GFS/2026/0001, GFS/2026/0005, GFS/9999/0001"
    r = client_a.post(COMPOSE, _data(_token(client_a), audience="students", students=numbers))
    html = r.content.decode()
    assert "GFS/2026/0005" in html and "GFS/9999/0001" in html
    assert "Unknown or inactive admission numbers" in html
    assert "Gone" not in html  # numbers only, never names
    assert _count() == 0


def test_more_than_50_students(client_a, w):
    login_verified(client_a, w.admin)
    numbers = "\n".join(f"GFS/2026/{n:04d}" for n in range(1, 52))
    r = client_a.post(COMPOSE, _data(_token(client_a), audience="students", students=numbers))
    assert "Enter at most 50 admission numbers." in r.content.decode()
    assert _count() == 0


def test_bursar_limited_to_debtors(client_a, w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        give_debt(w.a, w.term, 2_000_000)
        give_debt(w.c, w.term, 500_000)
    login_verified(client_a, w.bursar)
    token = _token(client_a)
    for audience in ("school", "level", "arm", "students", "bogus"):
        r = client_a.post(
            COMPOSE, _data(token, audience=audience, arm=w.arm_a.pk, level=w.arm_a.level_id)
        )
        assert r.status_code == 403, audience
        r = client_a.post(COMPOSE, _data(token, audience=audience, action="preview"))
        assert r.status_code == 403, audience
    assert _count() == 0

    data = _data(token, audience="debtors", term=w.term.pk, min_amount="10,000")
    r = client_a.post(COMPOSE, {**data, "action": "preview"})
    assert (r.context["preview"]["email_count"], r.context["preview"]["inapp_count"]) == (1, 1)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(COMPOSE, data)
    assert r.status_code == 302
    with schema_context("school_a"):
        msg = Message.objects.get()
        assert msg.audience_label == "Debtors ≥ ₦10,000.00 — 1st Term 2026/2027"
        assert list(msg.deliveries.values_list("student_id", flat=True).distinct()) == [w.a.pk]
    # The bursar's form offers only the debtors audience.
    r = client_a.get(COMPOSE)
    assert [v for v, _ in r.context["form"].fields["audience"].choices] == ["debtors"]


def test_debtors_minimum_must_be_naira(client_a, w):
    login_verified(client_a, w.admin)
    r = client_a.post(
        COMPOSE, _data(_token(client_a), audience="debtors", term=w.term.pk, min_amount="12,50")
    )
    assert r.status_code == 200
    assert "Enter the minimum balance in naira" in r.content.decode()


def test_debtors_term_must_be_in_current_session(client_a, w):
    login_verified(client_a, w.admin)
    with schema_context("school_a"):
        from datetime import date

        from apps.academics.models import AcademicSession, Term

        old = AcademicSession.objects.create(
            name="2025/2026", start_date=date(2025, 9, 1), end_date=date(2026, 7, 31)
        )
        old_term = Term.objects.create(
            session=old, number=1, start_date=date(2025, 9, 1), end_date=date(2025, 12, 15)
        )
    r = client_a.post(COMPOSE, _data(_token(client_a), audience="debtors", term=old_term.pk))
    assert r.status_code == 200
    assert "Select a valid choice" in r.content.decode()


@pytest.mark.parametrize("role", ["teacher", "proprietor"])
def test_teacher_and_proprietor_cannot_compose(client_a, w, role):
    client_a.force_login(getattr(w, role))
    assert client_a.get(COMPOSE).status_code == 403
    assert client_a.post(COMPOSE, _data("x")).status_code == 403
    assert _count() == 0


def test_student_and_anonymous_cannot_compose(client_a, w):
    r = client_a.get(COMPOSE)
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]
    client_a.force_login(w.a.user)
    assert client_a.get(COMPOSE).status_code == 403


def test_bursar_compose_redirect_errors_do_not_name_students(client_a, w):
    """A bursar's debtors audience with no debtors is a form error, not a crash."""
    login_verified(client_a, w.bursar)
    r = client_a.post(COMPOSE, _data(_token(client_a), audience="debtors", term=w.term.pk))
    assert r.status_code == 200
    assert "No students match this audience." in r.content.decode()


def test_staff_home_links(client_a, w):
    for role in ("admin", "principal", "bursar"):
        login_verified(client_a, getattr(w, role))
        assert reverse("comms:history") in client_a.get("/").content.decode(), role
        client_a.logout()
    client_a.force_login(w.proprietor)
    assert reverse("comms:history") in client_a.get("/").content.decode()
    client_a.force_login(w.teacher)
    assert reverse("comms:history") not in client_a.get("/").content.decode()
