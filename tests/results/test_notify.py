"""Guardian notifications when a class is published (no scores, counts-only audit)."""

import logging
import re
from smtplib import SMTPException
from unittest import mock

import pytest
from django.core import mail
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.core.logging import SECRET_ARG_TASKS
from apps.results.models import ClassResult, ResultSummary, SubjectResult
from apps.results.tasks import notify_published_task
from apps.results.workflow import publish_class
from tests.results.factories import accuracy_world, approve_and_compute

pytestmark = pytest.mark.django_db

BODY = "Results for 2026/2027 1st Term are ready. Log in to the student portal to view them."


@pytest.fixture
def w(school_a, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    with schema_context("school_a"):
        world = accuracy_world()
        s = world.students
        # A and B are siblings: one guardian, emails differing only in case, one phone.
        contacts = {
            "A": ("Parent.AB@example.com", "+2348030000001", "", None),
            "B": ("parent.ab@EXAMPLE.com", "+2348030000001", "", None),
            "C": ("c.mum@example.com", "+2348030000003", "c.dad@example.com", "+2348030000033"),
            "D": ("", "+2348030000004", "", None),
            "E": ("e.home@example.com", None, "", None),
            "F": ("f.home@example.com", "+2348030000006", "F.HOME@example.com", "+2348030000006"),
        }
        for letter, (email, phone, email2, phone2) in contacts.items():
            st = s[letter]
            st.guardian_email, st.guardian_phone = email, phone
            st.guardian2_email, st.guardian2_phone = email2, phone2
            if email2 or phone2:
                st.guardian2_name = "Second Guardian"
            st.save()
        approve_and_compute(world)
        yield world


EMAILS = {
    "parent.ab@example.com",
    "c.mum@example.com",
    "c.dad@example.com",
    "e.home@example.com",
    "f.home@example.com",
}
PHONES = {
    "+2348030000001",
    "+2348030000003",
    "+2348030000033",
    "+2348030000004",
    "+2348030000006",
}


def publish(w, capture):
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async") as cards,
        capture(execute=True),
    ):
        publish_class(w.arm, w.term, by=w.principal)
    cards.assert_called_once()


def score_strings(w):
    """Every total, average and subject total of the class, as shown anywhere."""
    out = set()
    for s in ResultSummary.objects.filter(arm=w.arm, term=w.term):
        out |= {str(s.total), str(s.average), str(int(s.total))}
    for r in SubjectResult.objects.filter(term=w.term):
        out |= {str(r.total), str(int(r.total))}
    return out


def test_publish_notifies_each_guardian_once_without_scores(
    w, django_capture_on_commit_callbacks, sms_outbox
):
    publish(w, django_capture_on_commit_callbacks)

    assert len(mail.outbox) == len(EMAILS)
    assert {m.to[0].lower() for m in mail.outbox} == EMAILS
    assert all(len(m.to) == 1 and not m.cc and not m.bcc for m in mail.outbox)
    scores = score_strings(w)
    for m in mail.outbox:
        assert m.body.strip() == BODY
        text = f"{m.subject} {m.body}"
        for number in re.findall(r"\d+(?:\.\d+)?", text):
            assert number not in scores, number
        assert "Pupil" not in text

    assert {m.to for m in sms_outbox} == PHONES
    assert len(sms_outbox) == len(PHONES)
    assert all(m.body == BODY for m in sms_outbox)

    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == {"emails_sent": 5, "sms_queued": 5, "failures": 0}
    assert ClassResult.objects.get(arm=w.arm, term=w.term).status == "published"


def test_publish_queues_cards_and_notifications(w, django_capture_on_commit_callbacks):
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async") as cards,
        mock.patch("apps.results.tasks.notify_published_task.apply_async") as notify,
        django_capture_on_commit_callbacks(execute=True),
    ):
        publish_class(w.arm, w.term, by=w.principal)
    cards.assert_called_once_with(args=[w.arm.pk, w.term.pk], queue="reports")
    notify.assert_called_once_with(args=[w.arm.pk, w.term.pk], queue="notifications")
    assert not mail.outbox


def test_no_sms_when_not_configured(w, settings, django_capture_on_commit_callbacks):
    settings.SMS_BACKEND = None
    publish(w, django_capture_on_commit_callbacks)
    assert len(mail.outbox) == len(EMAILS)
    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == {"emails_sent": 5, "sms_queued": 0, "failures": 0}


def test_send_failures_are_logged_and_never_raised(
    w, django_capture_on_commit_callbacks, sms_outbox, caplog
):
    from django.core.mail import send_mail as real_send_mail

    calls = []

    def flaky(*args, **kwargs):
        calls.append(kwargs["recipient_list"])
        if len(calls) == 1:
            raise SMTPException("refused for c.mum@example.com")
        return real_send_mail(*args, **kwargs)

    def sms_flaky(to, body):
        if to.endswith("0004"):
            raise ConnectionError(f"provider down for {to}")
        from apps.core.sms import outbox

        outbox.append(mock.Mock(to=to, body=body))

    with (
        mock.patch("apps.results.notify.send_mail", side_effect=flaky),
        mock.patch("apps.results.notify.send_sms", side_effect=sms_flaky),
        caplog.at_level(logging.WARNING),
    ):
        publish(w, django_capture_on_commit_callbacks)

    assert len(calls) == len(EMAILS)
    assert len(mail.outbox) == len(EMAILS) - 1
    assert len(sms_outbox) == len(PHONES) - 1
    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == {"emails_sent": 4, "sms_queued": 4, "failures": 2}
    assert ClassResult.objects.get(arm=w.arm, term=w.term).status == "published"
    logged = caplog.text
    assert "SMTPException" in logged and "ConnectionError" in logged
    assert "example.com" not in logged and "0004" not in logged and "Pupil" not in logged


def test_task_does_nothing_unless_still_published(w, sms_outbox):
    ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="approved")
    notify_published_task.delay(w.arm.pk, w.term.pk)
    assert not mail.outbox and not sms_outbox
    assert not AuditEvent.objects.filter(action="results.published_notified").exists()


def test_task_ignores_unknown_ids(w):
    notify_published_task.delay(w.arm.pk + 999, w.term.pk)
    assert not mail.outbox


def test_task_args_are_redacted_in_logs():
    assert notify_published_task.name == "apps.results.tasks.notify_published_task"
    assert notify_published_task.name in SECRET_ARG_TASKS


def test_sending_stops_when_the_class_is_unlocked_part_way(
    w, django_capture_on_commit_callbacks, sms_outbox
):
    from django.core.mail import send_mail as real_send_mail

    def unlock_after_first(*args, **kwargs):
        ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="draft")
        return real_send_mail(*args, **kwargs)

    with mock.patch("apps.results.notify.send_mail", side_effect=unlock_after_first):
        publish(w, django_capture_on_commit_callbacks)

    assert len(mail.outbox) == 1
    assert not sms_outbox
    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == {"emails_sent": 1, "sms_queued": 0, "failures": 0}
