"""Guardian notifications when a class is published (no scores, counts-only audit)."""

import logging
import re
from smtplib import SMTPException
from unittest import mock

import pytest
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend as LocmemBackend
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.comms.models import Delivery, Message
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

    # P6: the results message is email and in-app only, even with SMS configured.
    assert sms_outbox == []

    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == audit_counts(w, emails=5)
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


def audit_counts(w, *, emails):
    """The counts-only audit payload: the message id and its email and in-app deliveries
    (one in-app delivery per ranked student)."""
    msg = Message.objects.get(kind="results_published")
    return {"message_id": msg.pk, "email_count": emails, "inapp_count": len(w.students)}


def test_no_sms_when_not_configured(w, settings, django_capture_on_commit_callbacks):
    settings.SMS_BACKEND = None
    publish(w, django_capture_on_commit_callbacks)
    assert len(mail.outbox) == len(EMAILS)
    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == audit_counts(w, emails=5)


def test_send_failures_are_logged_and_never_raised(
    w, django_capture_on_commit_callbacks, sms_outbox, caplog
):
    """Email now goes out in ``comms`` batches: a refused address marks that delivery failed
    (logged by type only) and the rest still go."""
    calls = []

    def flaky(self, messages):
        calls.append(messages[0].to)
        if messages[0].to == ["c.mum@example.com"]:
            raise SMTPException("refused for c.mum@example.com")
        mail.outbox.extend(messages)
        return len(messages)

    with (
        mock.patch.object(LocmemBackend, "send_messages", autospec=True, side_effect=flaky),
        caplog.at_level(logging.WARNING),
    ):
        publish(w, django_capture_on_commit_callbacks)

    assert len(calls) == len(EMAILS)
    assert len(mail.outbox) == len(EMAILS) - 1
    assert not sms_outbox
    failed = Delivery.objects.get(status="failed")
    assert (failed.email, failed.error_type) == ("c.mum@example.com", "SMTPException")
    assert Delivery.objects.filter(channel="email", status="sent").count() == len(EMAILS) - 1
    event = AuditEvent.objects.get(action="results.published_notified")
    assert event.after == audit_counts(w, emails=5)
    assert ClassResult.objects.get(arm=w.arm, term=w.term).status == "published"
    logged = caplog.text
    assert "SMTPException" in logged
    assert "example.com" not in logged and "Pupil" not in logged


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


def test_class_unlocked_before_the_message_is_created_sends_nothing(
    w, django_capture_on_commit_callbacks, sms_outbox
):
    """The still-published re-check runs under the class's results lock: an unlock that
    commits while the task waits for the lock stops the message. (Email is now sent in
    ``comms`` batches after the message is created, so the old per-recipient stop is gone.)"""
    from apps.core.locks import school_lock as real_lock

    def unlocked_meanwhile(name):
        real_lock(name)
        ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="draft")

    with mock.patch("apps.results.notify.school_lock", side_effect=unlocked_meanwhile):
        publish(w, django_capture_on_commit_callbacks)

    assert not mail.outbox and not sms_outbox
    assert not Message.objects.exists()
    assert not AuditEvent.objects.filter(action="results.published_notified").exists()


def test_published_class_is_messaged_once(w, django_capture_on_commit_callbacks):
    publish(w, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        notify_published_task.delay(w.arm.pk, w.term.pk)
    assert Message.objects.filter(kind="results_published").count() == 1
    assert len(mail.outbox) == len(EMAILS)
    assert AuditEvent.objects.filter(action="results.published_notified").count() == 1


def _republished(w):
    """Unlock then publish again: a new publish event with a later ``published_at``."""
    from datetime import timedelta

    klass = ClassResult.objects.get(arm=w.arm, term=w.term)
    ClassResult.objects.filter(pk=klass.pk).update(status="draft")
    ClassResult.objects.filter(pk=klass.pk).update(
        status="published", published_at=klass.published_at + timedelta(hours=1)
    )


def test_retry_or_regenerate_of_the_same_publish_sends_nothing(
    w, django_capture_on_commit_callbacks
):
    from apps.results import workflow

    publish(w, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        notify_published_task.delay(w.arm.pk, w.term.pk)  # a retried task
    ClassResult.objects.filter(arm=w.arm, term=w.term).update(
        cards_error=ClassResult.NOTIFY_START_ERROR
    )
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        workflow.regenerate_report_cards(w.arm, w.term, by=w.admin)  # re-queues notifications
    assert Message.objects.filter(kind="results_published").count() == 1
    assert len(mail.outbox) == len(EMAILS)


def test_republish_after_unlock_notifies_again(w, django_capture_on_commit_callbacks):
    publish(w, django_capture_on_commit_callbacks)
    _republished(w)
    with django_capture_on_commit_callbacks(execute=True):
        notify_published_task.delay(w.arm.pk, w.term.pk)
    msgs = list(Message.objects.filter(kind="results_published").order_by("pk"))
    assert len(msgs) == 2
    assert msgs[0].dedupe_key != msgs[1].dedupe_key
    assert len(mail.outbox) == 2 * len(EMAILS)
    assert all(m.body == BODY for m in mail.outbox)
    assert AuditEvent.objects.filter(action="results.published_notified").count() == 2
