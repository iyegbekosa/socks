"""P6 Task 3: the results, receipt and absence messages go through ``notify``.

Each sender makes one ``Message`` of its kind with exactly the old text, with email and in-app
deliveries, and its own once-only guard still holds on a repeat trigger (Review Focus 5).
"""

from datetime import UTC, datetime
from unittest import mock

import cloudinary
import pytest
from django.core import mail
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.attendance import hooks as attendance_hooks
from apps.attendance import tasks as attendance_tasks
from apps.attendance.models import AttendanceRecord, AttendanceSettings
from apps.comms.models import Delivery, Message
from apps.fees import tasks as fee_tasks
from apps.results.tasks import notify_published_task
from tests.attendance.factories import make_register_world, today, version_of
from tests.fees.test_receipts import BODY as RECEIPT_BODY
from tests.fees.test_receipts import _paid_world
from tests.results.test_cards import Uploads
from tests.results.test_notify import BODY as RESULTS_BODY
from tests.results.test_notify import EMAILS as RESULTS_EMAILS
from tests.results.test_notify import publish
from tests.results.test_notify import w as results_world  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

ABSENCE_DATE = "2026-01-12"
ABSENCE_BODY = (
    "Ada was marked absent from school on 12 January 2026. "
    "Please contact the school if this is unexpected."
)


def _channels(msg):
    return {ch: list(msg.deliveries.filter(channel=ch).order_by("pk")) for ch in ("email", "inapp")}


# --- results published ----------------------------------------------------------------------


def test_results_published_goes_through_notify_once(
    results_world,  # noqa: F811
    django_capture_on_commit_callbacks,
    sms_outbox,
):
    w = results_world
    publish(w, django_capture_on_commit_callbacks)

    (msg,) = Message.objects.all()
    assert msg.kind == "results_published"
    assert msg.subject == "Results are ready"
    assert msg.body == RESULTS_BODY
    assert msg.related_type == "results.classresult"
    d = _channels(msg)
    assert {x.email for x in d["email"]} == RESULTS_EMAILS
    assert all(x.body == RESULTS_BODY and x.status == "sent" for x in d["email"])
    ranked = {s.user_id for s in w.students.values()} - {None}
    assert {x.user_id for x in d["inapp"]} == ranked
    assert all(x.body == RESULTS_BODY for x in d["inapp"])
    assert sorted(m.to[0].lower() for m in mail.outbox) == sorted(RESULTS_EMAILS)
    assert all(m.subject == "Results are ready" and m.body == RESULTS_BODY for m in mail.outbox)
    assert sms_outbox == []  # SMS dropped from the results path even though it is configured

    # A repeat trigger (the task delivered again) adds no second message.
    with django_capture_on_commit_callbacks(execute=True):
        notify_published_task.delay(w.arm.pk, w.term.pk)
    assert Message.objects.count() == 1
    assert len(mail.outbox) == len(RESULTS_EMAILS)


# --- receipts -------------------------------------------------------------------------------


@pytest.fixture
def uploads():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    u = Uploads()
    with (
        mock.patch("cloudinary.uploader.upload", side_effect=u.upload),
        mock.patch("cloudinary.uploader.destroy", side_effect=u.destroy),
    ):
        yield u
    cloudinary.config(**old)


@pytest.mark.pdf
def test_receipt_goes_through_notify_once(school_a, uploads, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        w = _paid_world()
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            fee_tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
        body = RECEIPT_BODY.format(year=w.receipt.year)

        (msg,) = Message.objects.all()
        assert msg.kind == "receipt"
        assert msg.subject == "Payment received"
        d = _channels(msg)
        assert [x.email for x in d["email"]] == ["guardian@example.com"]
        assert [x.user_id for x in d["inapp"]] == [w.student.user_id]
        assert all(x.body == body for x in d["email"] + d["inapp"])
        assert [(m.subject, m.body) for m in mail.outbox] == [("Payment received", body)]

        # Repeat triggers: regenerating the PDF, and the send task delivered again.
        with django_capture_on_commit_callbacks(execute=True):
            fee_tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
            fee_tasks.send_receipt_task.apply_async(args=[w.payment.pk], queue="notifications")
        assert Message.objects.count() == 1
        assert len(mail.outbox) == 1


LATE = datetime(2026, 10, 14, 21, 30, tzinfo=UTC)  # 22:30 in Lagos: inside quiet hours


def _at_night():
    """Run ``notify`` at 22:30 Lagos (overrides the suite's daytime pin)."""
    return mock.patch("apps.comms.service.clock", return_value=LATE)


def test_quiet_hours_window_contains_late():
    from datetime import time

    from apps.comms import service

    assert service.quiet_send_after(LATE, time(21, 0), time(7, 0)) is not None


def test_receipt_ignores_quiet_hours(school_a, django_capture_on_commit_callbacks):
    from apps.fees.receipts import send_receipt

    with schema_context("school_a"):
        w = _paid_world()
        mail.outbox.clear()
        with _at_night(), django_capture_on_commit_callbacks(execute=True):
            send_receipt(w.payment.pk)
        (msg,) = Message.objects.filter(kind="receipt")
        assert all(x.send_after is None for x in _channels(msg)["email"])
        assert len(mail.outbox) == 1


def test_results_published_at_night_is_held_until_seven(
    results_world,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    with _at_night():
        publish(results_world, django_capture_on_commit_callbacks)
    (msg,) = Message.objects.filter(kind="results_published")
    d = _channels(msg)
    assert mail.outbox == []
    assert d["email"] and all(x.status == "queued" and x.send_after for x in d["email"])
    assert all(x.status == "sent" for x in d["inapp"])  # in-app is never held


@pytest.mark.real_clock
def test_real_clock_marker_skips_the_daytime_pin():
    from apps.comms import service

    assert not isinstance(service.clock, mock.Mock)


def test_suite_pins_notify_to_daytime():
    from zoneinfo import ZoneInfo

    from apps.comms import service

    assert service.clock().astimezone(ZoneInfo("Africa/Lagos")).hour == 10


def test_results_message_failure_is_logged_and_never_raised(
    results_world,  # noqa: F811
    django_capture_on_commit_callbacks,
    caplog,
):
    import logging

    with (
        mock.patch("apps.results.notify.notify", side_effect=RuntimeError("secret Pupil")),
        caplog.at_level(logging.WARNING, logger="apps.results"),
    ):
        publish(results_world, django_capture_on_commit_callbacks)  # must not raise
    assert not Message.objects.exists() and mail.outbox == []
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text and "Pupil" not in caplog.text


# --- absence --------------------------------------------------------------------------------


@pytest.fixture
def aw(school_a):
    with schema_context("school_a"):
        w = make_register_world()
        a = w.students[0]
        a.first_name = "Ada"
        a.guardian_email = "Mum@Example.com"
        a.guardian2_email = "mum@example.com"
        a.save()
        yield w


def _save(client, w, status):
    client.force_login(w.teacher)
    data = {f"s_{s.pk}": "present" for s in w.students}
    data[f"s_{w.students[0].pk}"] = status
    data["version"] = version_of(w.arm)
    with today():
        r = client.post(reverse("attendance:register", args=[w.arm.pk, ABSENCE_DATE]), data)
    assert r.status_code == 302


def test_absence_goes_through_notify_once(client_a, aw, django_capture_on_commit_callbacks):
    a = aw.students[0]
    mail.outbox.clear()
    with schema_context("school_a"):
        with django_capture_on_commit_callbacks(execute=True):
            _save(client_a, aw, "absent")
        (msg,) = Message.objects.all()
        assert msg.kind == "absence"
        assert msg.subject == "Absence: Ada"
        assert msg.related_type == "attendance.attendancerecord"
        d = _channels(msg)
        assert [x.email for x in d["email"]] == ["mum@example.com"]
        assert [x.user_id for x in d["inapp"]] == [a.user_id]
        assert all(x.body == ABSENCE_BODY for x in d["email"] + d["inapp"])
        assert [(m.subject, m.body) for m in mail.outbox] == [("Absence: Ada", ABSENCE_BODY)]

        # Repeat triggers: corrected to present, then absent again, then the task re-run.
        for status in ("present", "absent"):
            with django_capture_on_commit_callbacks(execute=True):
                _save(client_a, aw, status)
        rid = AttendanceRecord.objects.get(student=a).pk
        with django_capture_on_commit_callbacks(execute=True):
            attendance_tasks.send_absence_emails_task.apply_async(args=[[rid]])
        assert Message.objects.count() == 1
        assert len(mail.outbox) == 1


def test_absence_email_is_delayed_by_the_setting(school_a):
    with schema_context("school_a"):
        assert AttendanceSettings.load().absence_email_delay_minutes == 10
        with mock.patch.object(attendance_tasks.send_absence_emails_task, "apply_async") as enq:
            attendance_hooks.on_register_saved([5, 6])
        enq.assert_called_once_with(args=[[5, 6]], queue="notifications", countdown=600)

        AttendanceSettings.objects.filter(pk=1).update(absence_email_delay_minutes=0)
        with mock.patch.object(attendance_tasks.send_absence_emails_task, "apply_async") as enq:
            attendance_hooks.on_register_saved([5])
        enq.assert_called_once_with(args=[[5]], queue="notifications")


def test_absence_corrected_before_the_delay_sends_nothing(
    client_a, aw, django_capture_on_commit_callbacks
):
    mail.outbox.clear()
    with schema_context("school_a"):
        with mock.patch.object(attendance_tasks.send_absence_emails_task, "apply_async") as enq:
            with django_capture_on_commit_callbacks(execute=True):
                _save(client_a, aw, "absent")
        (rid,) = enq.call_args.kwargs["args"][0]
        assert enq.call_args.kwargs["countdown"] == 600
        # Corrected to present before the delayed task runs.
        AttendanceRecord.objects.filter(pk=rid).update(status="present")
        with django_capture_on_commit_callbacks(execute=True):
            # The delayed run (eager Celery ignores the countdown, so run it now).
            attendance_tasks.send_absence_emails_task.apply_async(args=[[rid]])
        assert not Message.objects.exists()
        assert not Delivery.objects.exists()
        assert AttendanceRecord.objects.get(pk=rid).absence_emailed_at is None
    assert mail.outbox == []


def test_absence_with_no_delay_sends_at_once(client_a, aw, django_capture_on_commit_callbacks):
    mail.outbox.clear()
    with schema_context("school_a"):
        AttendanceSettings.objects.filter(pk=1).update(absence_email_delay_minutes=0)
        AttendanceSettings.load()
        with django_capture_on_commit_callbacks(execute=True):
            _save(client_a, aw, "absent")
    assert [m.body for m in mail.outbox] == [ABSENCE_BODY]


def test_delay_setting_range_and_form(client_a, school_a):
    from django.db import IntegrityError, transaction

    from tests.accounts.helpers import login_verified
    from tests.fees.factories import make_staff

    with schema_context("school_a"):
        conf = AttendanceSettings.load()
        with pytest.raises(IntegrityError), transaction.atomic():
            AttendanceSettings.objects.filter(pk=conf.pk).update(absence_email_delay_minutes=121)
        admin = make_staff("school_admin")
    login_verified(client_a, admin)
    base = {"late_threshold": "3", "edit_window_days": "7", "absence_email": "on"}
    assert b"absence_email_delay_minutes" in client_a.get("/attendance/settings/").content
    r = client_a.post("/attendance/settings/", {**base, "absence_email_delay_minutes": "121"})
    assert r.status_code == 200
    r = client_a.post("/attendance/settings/", {**base, "absence_email_delay_minutes": "30"})
    assert r.status_code == 302
    with schema_context("school_a"):
        assert AttendanceSettings.load().absence_email_delay_minutes == 30


# --- dedupe key (Ruling AG) -----------------------------------------------------------------


def test_dedupe_key_unique_per_kind_when_set(school_a):
    from django.db import IntegrityError, transaction

    with schema_context("school_a"):
        Message.objects.create(kind="announcement", subject="S", body="B")
        Message.objects.create(kind="announcement", subject="S", body="B")  # blank: repeatable
        Message.objects.create(kind="receipt", subject="S", body="B", dedupe_key="receipt:1")
        Message.objects.create(kind="absence", subject="S", body="B", dedupe_key="receipt:1")
        with pytest.raises(IntegrityError), transaction.atomic():
            Message.objects.create(kind="receipt", subject="S", body="B", dedupe_key="receipt:1")


@pytest.mark.pdf
def test_receipt_message_carries_its_dedupe_key(
    school_a, uploads, django_capture_on_commit_callbacks
):
    with schema_context("school_a"):
        w = _paid_world()
        with django_capture_on_commit_callbacks(execute=True):
            fee_tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
        assert Message.objects.get(kind="receipt").dedupe_key == f"receipt:{w.receipt.pk}"


def test_absence_ignores_quiet_hours(client_a, aw, django_capture_on_commit_callbacks):
    mail.outbox.clear()
    with schema_context("school_a"):
        AttendanceSettings.objects.filter(pk=1).update(absence_email_delay_minutes=0)
        AttendanceSettings.load()
        with _at_night(), django_capture_on_commit_callbacks(execute=True):
            _save(client_a, aw, "absent")
        (msg,) = Message.objects.filter(kind="absence")
        assert all(x.send_after is None for x in _channels(msg)["email"])
    assert [m.body for m in mail.outbox] == [ABSENCE_BODY]


def test_delay_form_refuses_negative_and_keeps_value_when_omitted(client_a, school_a):
    from tests.accounts.helpers import login_verified
    from tests.fees.factories import make_staff

    with schema_context("school_a"):
        AttendanceSettings.load()
        AttendanceSettings.objects.filter(pk=1).update(absence_email_delay_minutes=25)
        admin = make_staff("school_admin")
    login_verified(client_a, admin)
    base = {"late_threshold": "3", "edit_window_days": "7", "absence_email": "on"}
    r = client_a.post("/attendance/settings/", {**base, "absence_email_delay_minutes": "-1"})
    assert r.status_code == 200
    r = client_a.post("/attendance/settings/", {**base, "late_threshold": "4"})
    assert r.status_code == 302
    with schema_context("school_a"):
        conf = AttendanceSettings.load()
        assert (conf.late_threshold, conf.absence_email_delay_minutes) == (4, 25)
