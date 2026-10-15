import smtplib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.mail.backends.locmem import EmailBackend as LocmemBackend
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, Term
from apps.audit.models import AuditEvent
from apps.comms import service, tasks
from apps.comms.audience import Audience, label, resolve
from apps.comms.models import CommsSettings, Delivery, Message
from apps.core.logging import SECRET_ARG_TASKS
from apps.students.models import Enrolment, Student
from apps.tenancy.models import School
from tests.fees.factories import make_terms
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

LAGOS = ZoneInfo("Africa/Lagos")
MUM = "mum@example.com"


def lagos(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LAGOS)


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        t = make_terms()
        Term.objects.filter(pk=t.term1.pk).update(is_current=True)
        a = StudentProfileFactory(first_name="Ada", guardian_email=MUM)
        b = StudentProfileFactory(first_name="Bola", guardian_email=MUM)
        # Saved emails are lower-cased; force a case difference between the siblings.
        Student.objects.filter(pk=b.pk).update(guardian_email="Mum@Example.COM")
        b.refresh_from_db()
        c = StudentProfileFactory(first_name="Chi", guardian_email="chi.dad@example.com")
        mail.outbox.clear()
        yield SimpleNamespace(terms=t, students=[a, b, c], a=a, b=b, c=c)


def _emails(msg):
    return list(msg.deliveries.filter(channel="email").order_by("pk"))


def _inapp(msg):
    return list(msg.deliveries.filter(channel="inapp").order_by("pk"))


def test_family_email_deduplicated_across_siblings(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        msg = service.notify(
            "announcement",
            students=w.students,
            subject="Sports day",
            body="Sports day at {school_name} is on Friday.",
            per_student=False,
            now=lagos(2026, 10, 14, 10),
        )
    emails = _emails(msg)
    assert sorted(d.email for d in emails) == ["chi.dad@example.com", MUM]
    assert len(_inapp(msg)) == 3
    assert {d.user_id for d in _inapp(msg)} == {s.user_id for s in w.students}
    assert all(d.status == "sent" and d.sent_at for d in _inapp(msg))
    assert sorted(m.to[0] for m in mail.outbox) == ["chi.dad@example.com", MUM]
    assert all(m.subject == "Sports day" for m in mail.outbox)
    assert all(m.body == "Sports day at School A is on Friday." for m in mail.outbox)
    assert {d.status for d in _emails(msg)} == {"sent"}


def test_per_student_message_emails_each_sibling(w, django_capture_on_commit_callbacks):
    balances = {w.a.pk: "₦5,000.00", w.b.pk: "₦7,500.00", w.c.pk: "₦1,000.00"}
    with django_capture_on_commit_callbacks(execute=True):
        msg = service.notify(
            "fee_reminder",
            students=w.students,
            subject="School fees",
            body="{student_first_name} owes {balance} for {term}.",
            per_student=True,
            context_for=lambda s: {"student_first_name": s.first_name, "balance": balances[s.pk]},
            now=lagos(2026, 10, 14, 10),
        )
    assert len(_emails(msg)) == 3
    assert len(_inapp(msg)) == 3
    bodies = sorted((m.to[0], m.body) for m in mail.outbox)
    assert bodies == [
        ("chi.dad@example.com", "Chi owes ₦1,000.00 for 2026/2027 1st Term."),
        (MUM, "Ada owes ₦5,000.00 for 2026/2027 1st Term."),
        (MUM, "Bola owes ₦7,500.00 for 2026/2027 1st Term."),
    ]
    inapp_bodies = {d.student_id: d.body for d in _inapp(msg)}
    assert inapp_bodies[w.b.pk] == "Bola owes ₦7,500.00 for 2026/2027 1st Term."


def test_student_without_guardian_email_gets_inapp_only(w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        d = StudentProfileFactory(first_name="Dayo")
    with django_capture_on_commit_callbacks(execute=True):
        msg = service.notify(
            "announcement", students=[d], subject="Hi", body="Hello.", per_student=False
        )
    assert _emails(msg) == []
    assert len(_inapp(msg)) == 1
    assert mail.outbox == []


def test_announcement_refuses_per_student_placeholder(w):
    with pytest.raises(ValidationError):
        service.notify(
            "announcement", students=w.students, subject="X", body="{balance}", per_student=False
        )
    assert not Message.objects.exists()


def test_subject_required_and_at_most_120(w):
    for subject in ["", "   ", "x" * 121]:
        with pytest.raises(ValidationError):
            service.notify(
                "announcement", students=w.students, subject=subject, body="B", per_student=False
            )
    assert not Message.objects.exists()


def test_quiet_hours_hold_bulk_email_only(w, django_capture_on_commit_callbacks):
    composed = lagos(2026, 10, 14, 22, 30)
    next_seven = lagos(2026, 10, 15, 7).astimezone(UTC)
    with mock.patch("django.utils.timezone.now", return_value=composed):
        with django_capture_on_commit_callbacks(execute=True):
            msg = service.notify(
                "announcement",
                students=w.students,
                subject="Notice",
                body="Hello.",
                per_student=False,
                now=composed,
            )
    assert mail.outbox == []
    emails = _emails(msg)
    assert len(emails) == 2
    assert all(d.status == "queued" and d.send_after == next_seven for d in emails)
    assert next_seven.tzinfo is not None
    assert all(d.status == "sent" for d in _inapp(msg))  # in-app is never held

    # A receipt at 22:30 goes immediately.
    with mock.patch("django.utils.timezone.now", return_value=composed):
        with django_capture_on_commit_callbacks(execute=True):
            receipt = service.notify(
                "receipt",
                students=[w.c],
                subject="Payment received",
                body="Thank you.",
                per_student=True,
                now=composed,
            )
    assert [m.to for m in mail.outbox] == [["chi.dad@example.com"]]
    assert _emails(receipt)[0].send_after is None
    mail.outbox.clear()

    # The 5-minute job sends nothing before 07:00 ...
    with mock.patch("django.utils.timezone.now", return_value=lagos(2026, 10, 15, 6, 55)):
        tasks.send_held_school_task.apply(headers={"schema_name": "school_a"})
    assert mail.outbox == []
    # ... and everything after.
    with mock.patch("django.utils.timezone.now", return_value=lagos(2026, 10, 15, 7, 0)):
        tasks.send_held_school_task.apply(headers={"schema_name": "school_a"})
    assert sorted(m.to[0] for m in mail.outbox) == ["chi.dad@example.com", MUM]
    assert {d.status for d in _emails(msg)} == {"sent"}


@pytest.mark.parametrize(
    ("composed", "expected"),
    [
        (lagos(2026, 10, 14, 20, 59), None),
        (lagos(2026, 10, 14, 21, 0), lagos(2026, 10, 15, 7)),
        (lagos(2026, 10, 15, 0, 30), lagos(2026, 10, 15, 7)),
        (lagos(2026, 10, 15, 6, 59), lagos(2026, 10, 15, 7)),
        (lagos(2026, 10, 15, 7, 0), None),
    ],
)
def test_quiet_hours_window_edges(w, composed, expected):
    msg = service.notify(
        "results_published",
        students=[w.c],
        subject="Results",
        body="Ready.",
        per_student=False,
        now=composed.astimezone(UTC),
    )
    got = _emails(msg)[0].send_after
    assert got == (expected.astimezone(UTC) if expected else None)


def _flaky_backend(bad: str):
    def send_messages(self, messages):
        if messages[0].to == [bad]:
            raise smtplib.SMTPRecipientsRefused({bad: (550, b"no")})
        mail.outbox.extend(messages)
        return len(messages)

    return mock.patch.object(
        LocmemBackend, "send_messages", autospec=True, side_effect=send_messages
    )


def test_smtp_failure_marks_only_that_delivery_failed(
    w, django_capture_on_commit_callbacks, caplog
):
    with _flaky_backend(MUM), django_capture_on_commit_callbacks(execute=True):
        msg = service.notify(
            "announcement", students=w.students, subject="S", body="Body.", per_student=False
        )
    by_email = {d.email: d for d in _emails(msg)}
    assert by_email[MUM].status == "failed"
    assert by_email[MUM].error_type == "SMTPRecipientsRefused"
    assert by_email[MUM].sent_at is None
    assert by_email["chi.dad@example.com"].status == "sent"
    assert [m.to for m in mail.outbox] == [["chi.dad@example.com"]]
    assert MUM not in caplog.text
    assert "SMTPRecipientsRefused" in caplog.text


def test_batch_uses_one_connection(w, django_capture_on_commit_callbacks):
    with mock.patch("apps.comms.service.get_connection", wraps=mail.get_connection) as conn:
        with django_capture_on_commit_callbacks(execute=True):
            service.notify(
                "announcement", students=w.students, subject="S", body="B", per_student=False
            )
    assert conn.call_count == 1
    assert len(mail.outbox) == 2


def test_batch_task_twice_sends_each_email_once(w):
    msg = service.notify(
        "announcement", students=w.students, subject="S", body="B", per_student=False
    )
    ids = [d.pk for d in _emails(msg)]
    for _ in range(2):
        tasks.send_email_batch_task.apply(args=[ids], headers={"schema_name": "school_a"})
    assert len(mail.outbox) == 2
    assert {d.status for d in _emails(msg)} == {"sent"}


def test_batch_task_skips_claimed_and_inapp_rows(w):
    msg = service.notify(
        "announcement", students=w.students, subject="S", body="B", per_student=False
    )
    with schema_context("school_a"):
        first = _emails(msg)[0]
        Delivery.objects.filter(pk=first.pk).update(status="sending", claimed_at=timezone.now())
        ids = [d.pk for d in msg.deliveries.all()]
    tasks.send_email_batch_task.apply(args=[ids], headers={"schema_name": "school_a"})
    assert len(mail.outbox) == 1
    with schema_context("school_a"):
        first.refresh_from_db()
    assert first.status == "sending"


def test_held_job_returns_stale_sending_rows_and_sends_them(w):
    msg = service.notify(
        "announcement", students=w.students, subject="S", body="B", per_student=False
    )
    with schema_context("school_a"):
        stale, fresh = _emails(msg)
        Delivery.objects.filter(pk=stale.pk).update(
            status="sending", claimed_at=timezone.now() - timedelta(minutes=31)
        )
        Delivery.objects.filter(pk=fresh.pk).update(
            status="sending", claimed_at=timezone.now() - timedelta(minutes=20)
        )
    tasks.send_held_school_task.apply(headers={"schema_name": "school_a"})
    with schema_context("school_a"):
        stale.refresh_from_db()
        fresh.refresh_from_db()
    assert stale.status == "sent"
    assert fresh.status == "sending"
    assert [m.to[0] for m in mail.outbox] == [stale.email]


def test_notify_enqueues_due_emails_in_chunks_of_100(w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        many = [StudentProfileFactory(guardian_email=f"g{i}@example.com") for i in range(205)]
    with mock.patch.object(tasks.send_email_batch_task, "apply_async") as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            service.notify("announcement", students=many, subject="S", body="B", per_student=False)
    sizes = [len(c.kwargs["args"][0]) for c in enqueue.call_args_list]
    assert sizes == [100, 100, 5]
    assert all(c.kwargs["queue"] == "notifications" for c in enqueue.call_args_list)


def test_nothing_enqueued_before_commit(w, django_capture_on_commit_callbacks):
    with mock.patch.object(tasks.send_email_batch_task, "apply_async") as enqueue:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            service.notify(
                "announcement", students=w.students, subject="S", body="B", per_student=False
            )
        assert enqueue.call_count == 0
        assert len(callbacks) == 1


def test_audit_holds_counts_only(w, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        msg = service.notify(
            "announcement",
            students=w.students,
            subject="Secret subject",
            body="Secret body text.",
            per_student=False,
        )
    event = AuditEvent.objects.get(action="comms.message_sent")
    assert event.after == {
        "message_id": msg.pk,
        "kind": "announcement",
        "email_count": 2,
        "inapp_count": 3,
    }
    assert event.object_type == "comms.message"
    assert "Secret" not in str(event.after) and "Secret" not in str(event.before)


def test_related_and_audience_label_stored(w):
    with schema_context("school_a"):
        arm = ClassArm.objects.get(level__name="JSS 1", suffix="A")
    msg = service.notify(
        "announcement",
        students=w.students,
        subject="S",
        body="B",
        per_student=False,
        related=arm,
        audience_label="JSS 1A",
    )
    assert (msg.related_type, msg.related_id, msg.audience_label) == (
        "academics.classarm",
        arm.pk,
        "JSS 1A",
    )


def test_delivery_unique_constraints(w):
    msg = service.notify("announcement", students=[w.c], subject="S", body="B", per_student=False)
    from django.db import IntegrityError, transaction

    with schema_context("school_a"):
        email = _emails(msg)[0]
        inapp = _inapp(msg)[0]
        with pytest.raises(IntegrityError), transaction.atomic():
            Delivery.objects.create(
                message=msg, channel="email", student=w.c, email=email.email, status="queued"
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            Delivery.objects.create(
                message=msg, channel="inapp", student=w.c, user=inapp.user, status="sent"
            )


# --- audiences -------------------------------------------------------------------------------


def test_school_audience_is_active_students_enrolled_this_session(w):
    with schema_context("school_a"):
        withdrawn = StudentProfileFactory(status=Student.Status.WITHDRAWN)
        left = StudentProfileFactory()
        Enrolment.objects.filter(student=left).update(status=Enrolment.Status.WITHDRAWN)
        got = {s.pk for s in resolve(Audience(type="school"))}
        assert got == {s.pk for s in w.students}
        assert withdrawn.pk not in got
        assert label(Audience(type="school")) == "School"


def test_level_and_arm_audiences(w):
    with schema_context("school_a"):
        arm = ClassArm.objects.get(level__name="JSS 1", suffix="A")
        arm_b = ClassArm.objects.create(level=arm.level, suffix="B")
        other = StudentProfileFactory(enrolment__arm=arm_b)
        assert {s.pk for s in resolve(Audience(type="arm", id=arm.pk))} == {
            s.pk for s in w.students
        }
        assert {s.pk for s in resolve(Audience(type="level", id=arm.level_id))} == {
            s.pk for s in [*w.students, other]
        }
        assert label(Audience(type="arm", id=arm.pk)) == "JSS 1A"
        assert label(Audience(type="level", id=arm.level_id)) == "JSS 1"


def test_students_audience_limit_and_label(w):
    with schema_context("school_a"):
        ids = [w.a.pk, w.b.pk, w.c.pk]
        Student.objects.filter(pk=w.c.pk).update(status=Student.Status.WITHDRAWN)
        assert {s.pk for s in resolve(Audience(type="students", ids=ids))} == {w.a.pk, w.b.pk}
        assert label(Audience(type="students", ids=ids)) == "3 students"
        assert label(Audience(type="students", ids=[w.a.pk])) == "1 student"
        with pytest.raises(ValidationError):
            resolve(Audience(type="students", ids=list(range(1, 52))))


def test_debtors_audience_active_only_and_label(w):
    with schema_context("school_a"):
        term = w.terms.term1
        Student.objects.filter(pk=w.c.pk).update(status=Student.Status.WITHDRAWN)
        w.c.refresh_from_db()
        rows = [SimpleNamespace(student=s) for s in (w.a, w.c)]
        aud = Audience(type="debtors", term=term.pk, min_balance_kobo=100_000)
        with mock.patch("apps.comms.audience.debtors", return_value=rows) as fn:
            assert [s.pk for s in resolve(aud)] == [w.a.pk]
        assert fn.call_args.kwargs == {"min_balance_kobo": 100_000}
        assert fn.call_args.args[0].pk == term.pk
        assert label(aud) == "Debtors ≥ ₦1,000.00 — 1st Term 2026/2027"


def test_unknown_audience_refused(w):
    with schema_context("school_a"):
        with pytest.raises(ValidationError):
            resolve(Audience(type="everyone"))


# --- settings, Beat, logging -----------------------------------------------------------------


def test_comms_settings_defaults(w):
    with schema_context("school_a"):
        conf = CommsSettings.load()
    assert conf.email_quiet_start.strftime("%H:%M") == "21:00"
    assert conf.email_quiet_end.strftime("%H:%M") == "07:00"
    assert conf.reminder_min_balance_kobo == 100_000
    assert conf.reminder_template == (
        "Dear parent, {student_first_name}'s school fees balance for {term} is {balance}. "
        "Please pay through the student portal or at the school bursary. Thank you."
    )


def test_beat_schedule(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["comms-send-held"]
    assert entry["task"] == "apps.comms.tasks.send_held_all_schools"
    assert entry["schedule"] == 300.0
    assert entry["options"] == {"queue": "notifications"}


def test_fan_out_enqueues_active_ready_schools_only(school_a, school_b):
    School.objects.filter(schema_name="school_b").update(is_active=False)
    with mock.patch.object(tasks.send_held_school_task, "apply_async") as enqueue:
        tasks.send_held_all_schools()
    schemas = [c.kwargs["headers"]["schema_name"] for c in enqueue.call_args_list]
    assert "school_a" in schemas
    assert "school_b" not in schemas
    assert "public" not in schemas
    assert all(c.kwargs["queue"] == "notifications" for c in enqueue.call_args_list)


def test_tasks_args_redacted():
    for task in (tasks.send_email_batch_task, tasks.send_held_school_task):
        assert task.name in SECRET_ARG_TASKS
    assert "apps.comms.tasks.send_held_all_schools" in SECRET_ARG_TASKS


# --- Final review fixes: Rulings AI (branding), AJ (connection outage), AK (claim ownership) ---


def test_email_from_names_the_school_and_replies_to_its_contact(w, settings):
    settings.DEFAULT_FROM_EMAIL = "Build2Learn <no-reply@build2learn.ng>"
    School.objects.filter(schema_name="school_a").update(contact_email="office@school-a.ng")
    try:
        msg = service.notify(
            "announcement", students=w.students, subject="S", body="Body.", per_student=False
        )
        with schema_context("school_a"):
            ids = [d.pk for d in _emails(msg)]
        tasks.send_email_batch_task.apply(args=[ids], headers={"schema_name": "school_a"})
    finally:
        School.objects.filter(schema_name="school_a").update(contact_email="")
    assert len(mail.outbox) == 2
    assert {m.from_email for m in mail.outbox} == {"School A <no-reply@build2learn.ng>"}
    assert all(m.reply_to == ["office@school-a.ng"] for m in mail.outbox)
    assert all(m.body == "Body." for m in mail.outbox)  # texts unchanged


def test_no_reply_to_without_school_contact_email(w, settings):
    settings.DEFAULT_FROM_EMAIL = "no-reply@build2learn.ng"
    msg = service.notify(
        "announcement", students=[w.c], subject="S", body="Body.", per_student=False
    )
    with schema_context("school_a"):
        ids = [d.pk for d in _emails(msg)]
    tasks.send_email_batch_task.apply(args=[ids], headers={"schema_name": "school_a"})
    assert mail.outbox[0].from_email == "School A <no-reply@build2learn.ng>"
    assert mail.outbox[0].reply_to == []


def test_connection_open_failure_requeues_and_held_job_sends_later(w, caplog):
    msg = service.notify(
        "announcement", students=w.students, subject="S", body="B", per_student=False
    )
    with schema_context("school_a"):
        ids = [d.pk for d in _emails(msg)]
    with mock.patch.object(LocmemBackend, "open", side_effect=smtplib.SMTPConnectError(421, "x")):
        result = tasks.send_email_batch_task.apply(
            args=[ids], headers={"schema_name": "school_a"}
        ).get()
    assert result == {"sent": 0, "failed": 0, "requeued": 2}
    assert mail.outbox == []
    with schema_context("school_a"):
        rows = _emails(msg)
    assert {(d.status, d.claimed_at, d.error_type) for d in rows} == {("queued", None, "")}
    assert caplog.text.count("SMTPConnectError") == 1
    tasks.send_held_school_task.apply(headers={"schema_name": "school_a"})
    with schema_context("school_a"):
        assert {d.status for d in _emails(msg)} == {"sent"}
    assert len(mail.outbox) == 2


def test_reclaimed_row_is_not_sent_or_overwritten_by_the_old_batch(w):
    msg = service.notify(
        "announcement", students=w.students, subject="S", body="B", per_student=False
    )
    with schema_context("school_a"):
        first, second = _emails(msg)
    later = timezone.now() + timedelta(minutes=31)
    sent_by_old = []
    real_send = LocmemBackend.send_messages

    def send_then_steal(self, messages):
        # While the old batch sends its first email, the sweep re-queues and a new batch
        # re-claims both rows (simulated by moving claimed_at forward).
        if not sent_by_old:
            Delivery.objects.filter(pk__in=[first.pk, second.pk]).update(claimed_at=later)
        sent_by_old.extend(m.to[0] for m in messages)
        return real_send(self, messages)

    with mock.patch.object(
        LocmemBackend, "send_messages", autospec=True, side_effect=send_then_steal
    ):
        result = tasks.send_email_batch_task.apply(
            args=[[first.pk, second.pk]], headers={"schema_name": "school_a"}
        ).get()
    # The first email was already handed over; its row now belongs to the new claim, so the old
    # batch neither marks it nor sends the second row.
    assert sent_by_old == [first.email]
    assert result == {"sent": 0, "failed": 0, "requeued": 0}
    with schema_context("school_a"):
        first.refresh_from_db()
        second.refresh_from_db()
    assert (first.status, first.claimed_at, first.sent_at) == ("sending", later, None)
    assert (second.status, second.claimed_at) == ("sending", later)


def test_stale_claim_is_thirty_minutes():
    assert service.STALE_CLAIM == timedelta(minutes=30)


def test_notify_refuses_unknown_channels(w):
    for bad in (("emial",), (), ("email", "sms")):
        with pytest.raises(ValueError):
            service.notify(
                "announcement",
                students=[w.a],
                subject="S",
                body="B",
                per_student=False,
                channels=bad,
            )
    with schema_context("school_a"):
        assert not Message.objects.exists()


def test_term_placeholder_has_one_wording(w):
    msg = service.notify(
        "announcement", students=[w.a], subject="S", body="Term: {term}.", per_student=False
    )
    assert _inapp(msg)[0].body == "Term: 2026/2027 1st Term."
    assert service.term_text(w.terms.term1) == "2026/2027 1st Term"
    assert service.term_text(None) == ""


def test_newline_in_school_name_does_not_break_email(w, settings):
    settings.DEFAULT_FROM_EMAIL = "no-reply@build2learn.ng"
    School.objects.filter(schema_name="school_a").update(name="School\r\nA")
    try:
        msg = service.notify(
            "announcement", students=[w.c], subject="S", body="Body.", per_student=False
        )
        with schema_context("school_a"):
            ids = [d.pk for d in _emails(msg)]
        tasks.send_email_batch_task.apply(args=[ids], headers={"schema_name": "school_a"})
    finally:
        School.objects.filter(schema_name="school_a").update(name="School A")
    assert mail.outbox[0].from_email == "School A <no-reply@build2learn.ng>"
