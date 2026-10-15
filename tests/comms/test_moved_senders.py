"""P6 Task 3: the results, receipt and absence messages go through ``notify``.

Each sender makes one ``Message`` of its kind with exactly the old text, with email and in-app
deliveries, and its own once-only guard still holds on a repeat trigger (Review Focus 5).
"""

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


def test_receipt_ignores_quiet_hours(school_a, django_capture_on_commit_callbacks):
    from datetime import UTC, datetime

    from apps.comms import service

    with schema_context("school_a"):
        w = _paid_world()
        mail.outbox.clear()
        late = datetime(2026, 10, 14, 21, 30, tzinfo=UTC)  # 22:30 in Lagos
        with (
            mock.patch("apps.comms.service.timezone.now", return_value=late),
            django_capture_on_commit_callbacks(execute=True),
        ):
            from apps.fees.receipts import send_receipt

            send_receipt(w.payment.pk)
        (msg,) = Message.objects.filter(kind="receipt")
        assert all(x.send_after is None for x in _channels(msg)["email"])
        assert len(mail.outbox) == 1
        assert service.quiet_send_after(late, *_quiet()) is not None


def _quiet():
    from apps.comms.models import CommsSettings

    c = CommsSettings.load()
    return c.email_quiet_start, c.email_quiet_end


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
