"""P6 Task 4: message history (``comms:history``), the message page and "Retry failed"."""

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.comms import service
from apps.comms.models import Delivery, Message
from apps.comms.views.history import mask_email
from tests.accounts.helpers import login_verified
from tests.comms.helpers import DAD, MUM, make_world

pytestmark = pytest.mark.django_db

HISTORY = "/messages/"


def _send(kind, students, by=None, subject=None, per_student=None):
    per = kind in service.PER_STUDENT_KINDS if per_student is None else per_student
    return service.notify(
        kind,
        students=students,
        subject=subject or f"{kind} subject",
        body="Hello from {school_name}.",
        per_student=per,
        by=by,
        audience_label="JSS 1A",
    )


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = make_world()
        everyone = [world.a, world.b, world.c, world.d]
        world.announce = _send("announcement", everyone, by=world.admin, subject="Admin notice")
        world.bursar_note = _send(
            "announcement", [world.a], by=world.bursar, subject="Bursar notice"
        )
        world.reminder = _send("fee_reminder", [world.a], subject="Fees reminder")
        world.receipt = _send("receipt", [world.c], subject="Payment received")
        world.results = _send("results_published", everyone, subject="Results are ready")
        mail.outbox.clear()
        yield world


def _subjects(response):
    return [m.subject for m in response.context["page"].object_list]


def test_mask_email():
    assert mask_email("mum@example.com") == "m***@example.com"
    assert mask_email("A@b.ng") == "A***@b.ng"
    assert mask_email("broken") == "***"
    assert mask_email("") == ""


@pytest.mark.parametrize("role", ["admin", "principal", "proprietor"])
def test_admin_principal_proprietor_see_everything(client_a, w, role):
    user = getattr(w, role)
    if role == "proprietor":
        client_a.force_login(user)
    else:
        login_verified(client_a, user)
    r = client_a.get(HISTORY)
    assert r.status_code == 200
    assert _subjects(r) == [
        "Results are ready",
        "Payment received",
        "Fees reminder",
        "Bursar notice",
        "Admin notice",
    ]
    for msg in (w.announce, w.reminder, w.results):
        assert client_a.get(reverse("comms:message", args=[msg.pk])).status_code == 200


def test_bursar_sees_fee_messages_and_own_announcements(client_a, w):
    login_verified(client_a, w.bursar)
    r = client_a.get(HISTORY)
    assert _subjects(r) == ["Payment received", "Fees reminder", "Bursar notice"]
    for msg in (w.announce, w.results):
        assert client_a.get(reverse("comms:message", args=[msg.pk])).status_code == 404
        assert client_a.post(reverse("comms:retry_failed", args=[msg.pk])).status_code == 404
    for msg in (w.bursar_note, w.reminder, w.receipt):
        assert client_a.get(reverse("comms:message", args=[msg.pk])).status_code == 200


def test_filter_by_kind_and_counts(client_a, w):
    with schema_context("school_a"):
        rows = w.announce.deliveries.filter(channel="email").order_by("email")
        Delivery.objects.filter(pk=rows[0].pk).update(status="failed", error_type="SMTPError")
        Delivery.objects.filter(pk=rows[1].pk).update(status="queued", sent_at=None)
        first_inapp = w.announce.deliveries.filter(channel="inapp").first()
        Delivery.objects.filter(pk=first_inapp.pk).update(read_at=timezone.now())
    login_verified(client_a, w.admin)
    r = client_a.get(HISTORY + "?kind=announcement")
    assert _subjects(r) == ["Bursar notice", "Admin notice"]
    row = r.context["page"].object_list[1]
    # Admin notice: 2 family emails (one failed, one queued) and 4 in-app (one read).
    assert (row.sent_count, row.queued_count, row.failed_count, row.read_count) == (4, 1, 1, 1)
    assert len(_subjects(client_a.get(HISTORY + "?kind=bogus"))) == 5
    assert _subjects(client_a.get(HISTORY + "?kind=receipt")) == ["Payment received"]


def test_history_paginates_at_25_with_bounded_queries(client_a, w):
    login_verified(client_a, w.admin)
    client_a.get(HISTORY)  # warm the session and caches
    with CaptureQueriesContext(connection) as few:
        client_a.get(HISTORY)
    with schema_context("school_a"):
        for i in range(25):
            _send("announcement", [w.a, w.c], by=w.admin, subject=f"Bulk {i}")
    with CaptureQueriesContext(connection) as many:
        r = client_a.get(HISTORY)
    assert len(many.captured_queries) == len(few.captured_queries)
    assert len(r.context["page"].object_list) == 25
    assert r.context["page"].paginator.count == 30
    assert len(client_a.get(HISTORY + "?page=2").context["page"].object_list) == 5


def test_message_page_never_shows_a_full_email(client_a, w):
    login_verified(client_a, w.admin)
    r = client_a.get(reverse("comms:message", args=[w.announce.pk]))
    html = r.content.decode()
    assert MUM not in html and DAD not in html
    assert "m***@example.com" in html and "c***@example.com" in html
    assert "Ada" in html and "JSS 1B" in html  # in-app rows: first name and class


def test_message_page_bad_ids(client_a, w):
    login_verified(client_a, w.admin)
    assert client_a.get("/messages/999999/").status_code == 404
    assert client_a.get("/messages/abc/").status_code == 404


def _fail_one_email(msg):
    rows = list(msg.deliveries.filter(channel="email").order_by("email"))
    Delivery.objects.filter(pk=rows[0].pk).update(
        status="failed", error_type="SMTPRecipientsRefused", claimed_at=timezone.now()
    )
    return rows[0], rows[1:]


def test_retry_failed_requeues_only_failed(client_a, w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        failed, others = _fail_one_email(w.announce)
        before = {d.pk: (d.status, d.sent_at) for d in others}
    login_verified(client_a, w.admin)
    url = reverse("comms:retry_failed", args=[w.announce.pk])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        r = client_a.post(url)
    assert r.status_code == 302 and r["Location"] == reverse("comms:message", args=[w.announce.pk])
    with schema_context("school_a"):
        failed.refresh_from_db()
        assert (failed.status, failed.error_type, failed.claimed_at) == ("queued", "", None)
        assert {d.pk: (d.status, d.sent_at) for d in others} == {
            d.pk: (d.status, d.sent_at) for d in Delivery.objects.filter(pk__in=before)
        }
        event = AuditEvent.objects.get(action="comms.retry_failed")
        assert event.after == {"message_id": w.announce.pk, "count": 1}
        assert event.actor_id == w.admin.pk
    assert len(callbacks) == 1
    with schema_context("school_a"):
        callbacks[0]()  # the batch is enqueued on commit and sends only the retried row
        failed.refresh_from_db()
        assert failed.status == "sent"
    assert [m.to for m in mail.outbox] == [[failed.email]]


def test_retry_with_nothing_failed_changes_nothing(client_a, w):
    login_verified(client_a, w.principal)
    r = client_a.post(reverse("comms:retry_failed", args=[w.announce.pk]), follow=True)
    assert "No failed emails to retry." in r.content.decode()
    with schema_context("school_a"):
        assert not AuditEvent.objects.filter(action="comms.retry_failed").exists()


def test_bursar_retries_fee_messages_only(client_a, w, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        failed, _ = _fail_one_email(w.reminder)
    login_verified(client_a, w.bursar)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(reverse("comms:retry_failed", args=[w.reminder.pk]))
    assert r.status_code == 302
    with schema_context("school_a"):
        failed.refresh_from_db()
        assert failed.status == "sent"


def test_retry_is_post_only_and_proprietor_cannot(client_a, w):
    with schema_context("school_a"):
        failed, _ = _fail_one_email(w.announce)
    url = reverse("comms:retry_failed", args=[w.announce.pk])
    client_a.force_login(w.proprietor)
    assert client_a.post(url).status_code == 403
    login_verified(client_a, w.admin)
    assert client_a.get(url).status_code == 405
    with schema_context("school_a"):
        failed.refresh_from_db()
        assert failed.status == "failed"


def test_teacher_and_student_forbidden_everywhere(client_a, w):
    urls = [HISTORY, reverse("comms:message", args=[w.announce.pk])]
    for user in (w.teacher, w.a.user):
        client_a.force_login(user)
        for url in urls:
            assert client_a.get(url).status_code == 403, url
        assert client_a.post(reverse("comms:retry_failed", args=[w.announce.pk])).status_code == 403


def test_anonymous_redirected_to_login(client_a, w):
    r = client_a.get(HISTORY)
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]


def test_new_announcement_link_only_for_composers(client_a, w):
    compose = reverse("comms:compose")
    login_verified(client_a, w.bursar)
    assert compose in client_a.get(HISTORY).content.decode()
    client_a.logout()
    client_a.force_login(w.proprietor)
    assert compose not in client_a.get(HISTORY).content.decode()


def test_system_messages_show_no_sender(client_a, w):
    login_verified(client_a, w.admin)
    html = client_a.get(HISTORY).content.decode()
    assert "System" in html
    with schema_context("school_a"):
        assert Message.objects.filter(created_by__isnull=True).count() == 3
