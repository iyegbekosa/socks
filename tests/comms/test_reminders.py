"""P6 Task 5: scheduled fee reminders, reminder dates and settings (``comms:reminders``)."""

from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import Term
from apps.audit.models import AuditEvent
from apps.comms import reminders, tasks
from apps.comms.models import DEFAULT_REMINDER_TEMPLATE, CommsSettings, FeeReminderDate, Message
from apps.core.logging import SECRET_ARG_TASKS
from apps.tenancy.models import School
from tests.accounts.helpers import login_verified
from tests.comms.helpers import give_debt, make_world

pytestmark = pytest.mark.django_db

URL = "/messages/reminders/"
EXACT = (
    "Dear parent, Ada's school fees balance for 1st Term 2026/2027 is ₦50,000.00. "
    "Please pay through the student portal or at the school bursary. Thank you."
)


def _monday_on_or_after(d):
    return d + timedelta(days=(7 - d.weekday()) % 7)


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = make_world()
        today = timezone.localdate()
        Term.objects.filter(pk=world.term.pk).update(
            start_date=today - timedelta(days=40), end_date=today + timedelta(days=100)
        )
        world.term.refresh_from_db()
        world.today = today
        world.monday = _monday_on_or_after(today + timedelta(days=7))
        give_debt(world.a, world.term, 5_000_000)
        give_debt(world.c, world.term, 50_000)  # ₦500, under the ₦1,000 minimum
        mail.outbox.clear()
        yield world


def _on(w, d=None):
    FeeReminderDate.objects.get_or_create(term=w.term, date=d or w.today)


def _text(m):
    return list(m.deliveries.filter(channel="email").values_list("body", flat=True))


# --- sending ----------------------------------------------------------------------------


def test_debtor_reminded_with_exact_text_and_small_balance_not(w):
    with schema_context("school_a"):
        _on(w)
        message = reminders.send_due_reminders(w.today)
        assert message.kind == "fee_reminder"
        assert message.subject == "School fees reminder"
        assert message.audience_label == "Debtors ≥ ₦1,000.00 — 1st Term 2026/2027"
        assert message.dedupe_key == f"fee_reminder:{w.today.isoformat()}"
        assert message.created_by is None
        students = set(message.deliveries.values_list("student__first_name", flat=True))
        assert students == {"Ada"}
        assert _text(message) == [EXACT]
        assert message.deliveries.filter(channel="inapp", student=w.a).exists()


def test_nothing_sent_on_a_non_reminder_date(w):
    with schema_context("school_a"):
        _on(w, w.monday)
        assert reminders.send_due_reminders(w.today) is None
        assert Message.objects.filter(kind="fee_reminder").count() == 0


def test_no_message_when_nobody_qualifies(w):
    with schema_context("school_a"):
        CommsSettings.objects.update_or_create(pk=1, defaults={"reminder_min_balance_kobo": 10**9})
        _on(w)
        assert reminders.send_due_reminders(w.today) is None
        assert Message.objects.count() == 0


def test_withdrawn_students_not_reminded(w):
    with schema_context("school_a"):
        give_debt(w.gone, w.term, 9_000_000)
        _on(w)
        message = reminders.send_due_reminders(w.today)
        assert not message.deliveries.filter(student=w.gone).exists()
        assert "gone@example.com" not in message.deliveries.values_list("email", flat=True)


def test_custom_template_and_minimum_are_used(w):
    with schema_context("school_a"):
        CommsSettings.objects.update_or_create(
            pk=1,
            defaults={"reminder_min_balance_kobo": 10_000, "reminder_template": "Owe {balance}."},
        )
        _on(w)
        message = reminders.send_due_reminders(w.today)
        assert set(message.deliveries.values_list("student__first_name", flat=True)) == {
            "Ada",
            "Chi",
        }
        assert sorted(_text(message)) == ["Owe ₦50,000.00.", "Owe ₦500.00."]


def test_reminders_idempotent_per_day(w):
    with schema_context("school_a"):
        _on(w)
        first = reminders.send_due_reminders(w.today)
        assert first is not None
        assert reminders.send_due_reminders(w.today) is None
        assert reminders.send_now(w.today, by=w.bursar) is None
        assert Message.objects.filter(kind="fee_reminder").count() == 1
        assert first.deliveries.filter(channel="email").count() == 1


def test_manual_send_counts_for_today_so_the_job_does_nothing(w):
    with schema_context("school_a"):
        _on(w)
        sent = reminders.send_now(w.today, by=w.bursar)
        assert sent is not None and sent.created_by == w.bursar
        assert reminders.send_due_reminders(w.today) is None
        assert Message.objects.filter(kind="fee_reminder").count() == 1


def test_next_day_sends_again(w):
    with schema_context("school_a"):
        _on(w)
        _on(w, w.today + timedelta(days=1))
        assert reminders.send_due_reminders(w.today) is not None
        assert reminders.send_due_reminders(w.today + timedelta(days=1)) is not None


# --- reminder dates ---------------------------------------------------------------------


def _post(client, **data):
    return client.post(URL, data)


def test_add_date_audited(client_a, w):
    login_verified(client_a, w.bursar)
    r = _post(client_a, action="add_date", date=w.monday.isoformat())
    assert r.status_code == 302
    with schema_context("school_a"):
        row = FeeReminderDate.objects.get(term=w.term, date=w.monday)
        ev = AuditEvent.objects.get(action="comms.reminder_date_saved")
    assert ev.after == {"id": row.pk, "date": w.monday.isoformat()}


def test_seventh_date_refused(client_a, w):
    with schema_context("school_a"):
        day = w.monday
        for _ in range(6):
            FeeReminderDate.objects.create(term=w.term, date=day)
            day += timedelta(days=7)
    login_verified(client_a, w.admin)
    r = _post(client_a, action="add_date", date=day.isoformat())
    assert r.status_code == 200
    assert "at most 6" in r.content.decode()
    with schema_context("school_a"):
        assert FeeReminderDate.objects.count() == 6


def test_past_date_refused(client_a, w):
    login_verified(client_a, w.bursar)
    past = _monday_on_or_after(w.today - timedelta(days=21))
    r = _post(client_a, action="add_date", date=past.isoformat())
    assert r.status_code == 200
    assert "past" in r.content.decode()
    with schema_context("school_a"):
        assert FeeReminderDate.objects.count() == 0


def test_today_allowed_when_a_school_day(client_a, w):
    with schema_context("school_a"):
        from apps.attendance.days import is_school_day

        ok = is_school_day(w.term, w.today)
    login_verified(client_a, w.bursar)
    r = _post(client_a, action="add_date", date=w.today.isoformat())
    assert (r.status_code == 302) == ok


def test_weekend_and_outside_term_refused(client_a, w):
    login_verified(client_a, w.bursar)
    saturday = w.monday + timedelta(days=5)
    outside = w.term.end_date + timedelta(days=3)
    for d in (saturday, outside):
        r = _post(client_a, action="add_date", date=d.isoformat())
        assert r.status_code == 200
        assert "school day" in r.content.decode()
    with schema_context("school_a"):
        assert FeeReminderDate.objects.count() == 0


def test_duplicate_date_is_a_form_error(client_a, w):
    with schema_context("school_a"):
        FeeReminderDate.objects.create(term=w.term, date=w.monday)
    login_verified(client_a, w.bursar)
    r = _post(client_a, action="add_date", date=w.monday.isoformat())
    assert r.status_code == 200
    assert "already" in r.content.decode()


def test_delete_date_post_only_and_audited(client_a, w):
    with schema_context("school_a"):
        row = FeeReminderDate.objects.create(term=w.term, date=w.monday)
    url = reverse("comms:reminder_date_delete", args=[row.pk])
    login_verified(client_a, w.bursar)
    assert client_a.get(url).status_code == 405
    assert client_a.post(url).status_code == 302
    with schema_context("school_a"):
        assert not FeeReminderDate.objects.filter(pk=row.pk).exists()
        ev = AuditEvent.objects.get(action="comms.reminder_date_deleted")
    assert ev.after == {"id": row.pk, "date": w.monday.isoformat()}


def test_past_date_cannot_be_deleted(client_a, w):
    with schema_context("school_a"):
        row = FeeReminderDate.objects.create(term=w.term, date=w.today - timedelta(days=3))
    login_verified(client_a, w.bursar)
    client_a.post(reverse("comms:reminder_date_delete", args=[row.pk]))
    with schema_context("school_a"):
        assert FeeReminderDate.objects.filter(pk=row.pk).exists()
        assert not AuditEvent.objects.filter(action="comms.reminder_date_deleted").exists()


# --- settings ---------------------------------------------------------------------------


def test_save_settings_audited_without_template_text(client_a, w):
    login_verified(client_a, w.bursar)
    r = _post(
        client_a, action="save_settings", min_balance="2,500", template="Owe {balance} ({term})."
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        conf = CommsSettings.load()
        ev = AuditEvent.objects.get(action="comms.reminder_settings_saved")
    assert conf.reminder_min_balance_kobo == 250_000
    assert conf.reminder_template == "Owe {balance} ({term})."
    assert ev.before == {"reminder_min_balance_kobo": 100_000}
    assert ev.after == {"reminder_min_balance_kobo": 250_000, "template_changed": True}
    assert "Owe" not in str(ev.before) + str(ev.after)


def test_unchanged_settings_record_nothing_changed(client_a, w):
    login_verified(client_a, w.bursar)
    _post(client_a, action="save_settings", min_balance="1000", template=DEFAULT_REMINDER_TEMPLATE)
    with schema_context("school_a"):
        assert not AuditEvent.objects.filter(action="comms.reminder_settings_saved").exists()


def test_template_with_unknown_placeholder_refused_with_exact_message(client_a, w):
    login_verified(client_a, w.bursar)
    r = _post(client_a, action="save_settings", min_balance="1000", template="Dear {name}")
    assert r.status_code == 200
    assert "Unknown placeholder {name}. Use only: " in r.content.decode()
    with schema_context("school_a"):
        assert CommsSettings.load().reminder_template == DEFAULT_REMINDER_TEMPLATE


@pytest.mark.parametrize("bad", ["-5", "abc", "10.505", ""])
def test_bad_minimum_refused(client_a, w, bad):
    login_verified(client_a, w.bursar)
    r = _post(client_a, action="save_settings", min_balance=bad, template="Owe {balance}")
    assert r.status_code == 200
    with schema_context("school_a"):
        assert CommsSettings.load().reminder_min_balance_kobo == 100_000


def test_reset_to_default(client_a, w):
    with schema_context("school_a"):
        CommsSettings.objects.update_or_create(
            pk=1, defaults={"reminder_template": "Owe {balance}"}
        )
    login_verified(client_a, w.admin)
    assert _post(client_a, action="reset_template").status_code == 302
    with schema_context("school_a"):
        assert CommsSettings.load().reminder_template == DEFAULT_REMINDER_TEMPLATE
        ev = AuditEvent.objects.get(action="comms.reminder_settings_saved")
    assert ev.after == {"template_changed": True}


# --- send now and permissions -----------------------------------------------------------


def test_send_now_view_then_job_is_a_no_op(client_a, w):
    login_verified(client_a, w.bursar)
    r = client_a.post(reverse("comms:reminders_send"))
    assert r.status_code == 302
    with schema_context("school_a"):
        assert Message.objects.filter(kind="fee_reminder").count() == 1
        assert reminders.send_due_reminders(w.today) is None
    client_a.post(reverse("comms:reminders_send"))
    with schema_context("school_a"):
        assert Message.objects.filter(kind="fee_reminder").count() == 1


@pytest.mark.parametrize("role", ["bursar", "admin"])
def test_managers_can_view_and_edit(client_a, w, role):
    login_verified(client_a, getattr(w, role))
    r = client_a.get(URL)
    assert r.status_code == 200
    assert "Send reminders now" in r.content.decode()
    assert "Reset to default" in r.content.decode()


@pytest.mark.parametrize("role", ["principal", "proprietor"])
def test_principal_and_proprietor_read_only(client_a, w, role):
    if role == "principal":
        login_verified(client_a, w.principal)
    else:
        client_a.force_login(w.proprietor)
    r = client_a.get(URL)
    assert r.status_code == 200
    html = r.content.decode()
    assert "Send reminders now" not in html and "Reset to default" not in html
    assert client_a.post(URL, {"action": "reset_template"}).status_code == 403
    assert client_a.post(reverse("comms:reminders_send")).status_code == 403
    with schema_context("school_a"):
        row = FeeReminderDate.objects.create(term=w.term, date=w.monday)
    assert client_a.post(reverse("comms:reminder_date_delete", args=[row.pk])).status_code == 403


def test_other_roles_403(client_a, w):
    client_a.force_login(w.teacher)
    assert client_a.get(URL).status_code == 403
    assert client_a.post(reverse("comms:reminders_send")).status_code == 403


def test_links_on_fees_dashboard_and_history(client_a, w):
    login_verified(client_a, w.bursar)
    assert URL in client_a.get("/fees/").content.decode()
    assert URL in client_a.get("/messages/").content.decode()


# --- Beat -------------------------------------------------------------------------------


def test_beat_schedule(settings):
    from celery.schedules import crontab

    entry = settings.CELERY_BEAT_SCHEDULE["comms-fee-reminders"]
    assert entry["task"] == "apps.comms.tasks.send_reminders_all_schools"
    assert entry["schedule"] == crontab(hour=8, minute=0)
    assert entry["options"] == {"queue": "notifications"}


def test_fan_out_active_schools_only_with_schema_headers(school_a, school_b):
    School.objects.filter(schema_name="school_b").update(is_active=False)
    with mock.patch.object(tasks.send_reminders_school_task, "apply_async") as enqueue:
        tasks.send_reminders_all_schools()
    schemas = [c.kwargs["headers"]["schema_name"] for c in enqueue.call_args_list]
    assert "school_a" in schemas
    assert "school_b" not in schemas
    assert "public" not in schemas
    assert all(c.kwargs["queue"] == "notifications" for c in enqueue.call_args_list)


def test_fan_out_skips_unprovisioned_schools(school_a, school_b):
    School.objects.filter(schema_name="school_b").update(provisioning_status="pending")
    with mock.patch.object(tasks.send_reminders_school_task, "apply_async") as enqueue:
        tasks.send_reminders_all_schools()
    schemas = [c.kwargs["headers"]["schema_name"] for c in enqueue.call_args_list]
    assert "school_b" not in schemas


def test_school_task_uses_local_date_and_sends(w):
    with schema_context("school_a"):
        _on(w)
        tasks.send_reminders_school_task.run()
        assert Message.objects.filter(kind="fee_reminder").count() == 1
        tasks.send_reminders_school_task.run()
        assert Message.objects.filter(kind="fee_reminder").count() == 1


def test_new_tasks_args_redacted():
    assert tasks.send_reminders_school_task.name in SECRET_ARG_TASKS
    assert "apps.comms.tasks.send_reminders_all_schools" in SECRET_ARG_TASKS
