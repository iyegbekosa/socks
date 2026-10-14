import logging
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.attendance import hooks, tasks
from apps.attendance.models import AttendanceRecord, AttendanceSettings
from tests.attendance.factories import make_register_world, today, version_of

pytestmark = pytest.mark.django_db

D = "2026-01-12"
BODY = (
    "{name} was marked absent from school on 12 January 2026. "
    "Please contact the school if this is unexpected."
)


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        w = make_register_world()
        for s, name in zip(w.students, ["Ada", "Bola", "Chi"], strict=True):
            s.first_name = name
            s.save()
        a, b, c = w.students
        a.guardian_email = "Mum@Example.com"
        a.guardian2_email = "mum@example.com"
        a.save()
        b.guardian_email = "bola.parent@example.com"
        b.save()
        yield w


def _save(client, w, overrides):
    client.force_login(w.teacher)
    data = {f"s_{s.pk}": overrides.get(s.pk, "present") for s in w.students}
    data["version"] = version_of(w.arm)
    with today():
        r = client.post(reverse("attendance:register", args=[w.arm.pk, D]), data)
    assert r.status_code == 302


def test_two_absent_students_get_one_email_per_unique_address(
    client_a, w, django_capture_on_commit_callbacks
):
    a, b, c = w.students
    mail.outbox.clear()
    with schema_context("school_a"):
        with django_capture_on_commit_callbacks(execute=True):
            _save(client_a, w, {a.pk: "absent", b.pk: "absent", c.pk: "excused"})
    assert sorted(m.to[0].lower() for m in mail.outbox) == [
        "bola.parent@example.com",
        "mum@example.com",
    ]
    bodies = {m.subject: m.body for m in mail.outbox}
    assert bodies == {
        "Absence: Ada": BODY.format(name="Ada"),
        "Absence: Bola": BODY.format(name="Bola"),
    }


def test_excused_student_gets_no_email(client_a, w, django_capture_on_commit_callbacks):
    b = w.students[1]
    mail.outbox.clear()
    with schema_context("school_a"):
        with django_capture_on_commit_callbacks(execute=True):
            _save(client_a, w, {b.pk: "excused"})
    assert mail.outbox == []


def test_setting_off_sends_nothing(client_a, w, django_capture_on_commit_callbacks):
    mail.outbox.clear()
    with schema_context("school_a"):
        conf = AttendanceSettings.load()
        conf.absence_email = False
        conf.save()
        with django_capture_on_commit_callbacks(execute=True):
            _save(client_a, w, {w.students[0].pk: "absent"})
        # The task itself also refuses when the setting is off.
        rid = AttendanceRecord.objects.get(student=w.students[0]).pk
        tasks.send_absence_emails_task.apply_async(args=[[rid]], queue="notifications")
    assert mail.outbox == []
    with schema_context("school_a"):
        assert AttendanceRecord.objects.get(pk=rid).absence_emailed_at is None


def test_absence_email_sent_once_per_student_per_date(
    client_a, w, django_capture_on_commit_callbacks
):
    a = w.students[0]
    mail.outbox.clear()
    with schema_context("school_a"):
        for status in ("absent", "present", "absent"):
            with django_capture_on_commit_callbacks(execute=True):
                _save(client_a, w, {a.pk: status})
        assert AttendanceRecord.objects.get(student=a).absence_emailed_at is not None
    assert len(mail.outbox) == 1


def test_student_without_guardian_email_is_skipped_silently(
    client_a, w, caplog, django_capture_on_commit_callbacks
):
    c = w.students[2]
    mail.outbox.clear()
    with caplog.at_level(logging.DEBUG, logger="apps.attendance"):
        with schema_context("school_a"):
            with django_capture_on_commit_callbacks(execute=True):
                _save(client_a, w, {c.pk: "absent"})
    assert mail.outbox == []
    assert not [r for r in caplog.records if r.name.startswith("apps.attendance")]


def test_smtp_failure_is_logged_by_type_and_save_unaffected(
    client_a, w, caplog, django_capture_on_commit_callbacks
):
    a = w.students[0]
    mail.outbox.clear()
    with mock.patch("apps.attendance.notify.send_mail", side_effect=OSError("boom Ada")):
        with caplog.at_level(logging.WARNING, logger="apps.attendance"):
            with schema_context("school_a"):
                with django_capture_on_commit_callbacks(execute=True):
                    _save(client_a, w, {a.pk: "absent"})
                rec = AttendanceRecord.objects.get(student=a)
    assert rec.status == "absent"
    assert rec.absence_emailed_at is not None  # no retry storm
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "OSError" in text and str(rec.pk) in text
    assert "boom" not in text and "Ada" not in text and "example.com" not in text


def test_no_sms_even_when_configured(client_a, w, django_capture_on_commit_callbacks):
    a = w.students[0]
    with (
        mock.patch("apps.core.sms.send_sms") as sms,
        mock.patch("apps.core.sms.sms_configured", return_value=True),
    ):
        with schema_context("school_a"):
            with django_capture_on_commit_callbacks(execute=True):
                _save(client_a, w, {a.pk: "absent"})
    sms.assert_not_called()


def test_enqueue_failure_is_logged_by_type(school_a, caplog):
    with schema_context("school_a"):
        with mock.patch.object(
            tasks.send_absence_emails_task, "apply_async", side_effect=ConnectionError("secret")
        ):
            with caplog.at_level(logging.WARNING, logger="apps.attendance"):
                hooks.on_register_saved([1, 2])
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in text and "secret" not in text


def test_task_is_redacted_and_on_notifications_queue():
    from apps.core.logging import SECRET_ARG_TASKS

    assert "apps.attendance.tasks.send_absence_emails_task" in SECRET_ARG_TASKS
    assert tasks.send_absence_emails_task.queue == "notifications"


def test_broker_failure_keeps_the_saved_register(
    client_a, w, caplog, django_capture_on_commit_callbacks
):
    """End to end: the save still redirects and persists when the broker is down."""
    a = w.students[0]
    with (
        mock.patch.object(
            tasks.send_absence_emails_task, "apply_async", side_effect=ConnectionError("down")
        ),
        caplog.at_level(logging.WARNING, logger="apps.attendance"),
        schema_context("school_a"),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            _save(client_a, w, {a.pk: "absent"})  # asserts the 302
        assert AttendanceRecord.objects.get(student=a).status == "absent"
    assert "ConnectionError" in " ".join(r.getMessage() for r in caplog.records)


def test_setting_off_hook_does_not_enqueue(client_a, w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        conf = AttendanceSettings.load()
        conf.absence_email = False
        conf.save()
        with mock.patch.object(tasks.send_absence_emails_task, "apply_async") as enqueue:
            with django_capture_on_commit_callbacks(execute=True):
                _save(client_a, w, {w.students[0].pk: "absent"})
    enqueue.assert_not_called()


def test_sending_twice_emails_once(client_a, w):
    """The claim guards a duplicate delivery of the task (or two workers)."""
    from apps.attendance.notify import send_absence_emails

    a = w.students[0]
    _save(client_a, w, {a.pk: "absent"})  # on_commit hooks do not run: nothing sent yet
    mail.outbox.clear()
    with schema_context("school_a"):
        rid = AttendanceRecord.objects.get(student=a).pk
        assert send_absence_emails([rid]) == 1
        assert send_absence_emails([rid]) == 0
    assert len(mail.outbox) == 1
