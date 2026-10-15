import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.comms import service
from apps.comms.inbox import unread_count
from apps.comms.models import Delivery
from tests.accounts.factories import StaffFactory
from tests.fees.factories import make_terms
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(school_a):
    from types import SimpleNamespace

    from apps.academics.models import Term

    with schema_context("school_a"):
        t = make_terms()
        Term.objects.filter(pk=t.term1.pk).update(is_current=True)
        a = StudentProfileFactory(first_name="Ada")
        b = StudentProfileFactory(first_name="Bola")
        m1 = service.notify(
            "announcement",
            students=[a, b],
            subject="Sports day",
            body="Come <b>early</b>\nBring water",
            per_student=False,
            channels=("inapp",),
        )
        m2 = service.notify(
            "announcement",
            students=[b],
            subject="Only Bola",
            body="Secret for Bola",
            per_student=False,
            channels=("inapp",),
        )
        da = Delivery.objects.get(message=m1, student=a)
        db = Delivery.objects.get(message=m2, student=b)
        yield SimpleNamespace(a=a, b=b, da=da, db=db, m1=m1)


def test_urls():
    assert reverse("comms:inbox") == "/inbox/"
    assert reverse("comms:inbox_item", args=[5]) == "/inbox/5/"
    assert reverse("comms:inbox_read_all") == "/inbox/read-all/"


def test_student_sees_only_own(client_a, w):
    client_a.force_login(w.a.user)
    r = client_a.get(reverse("comms:inbox"))
    assert r.status_code == 200
    html = r.content.decode()
    assert "Sports day" in html
    assert "Only Bola" not in html


def test_body_is_escaped_with_line_breaks(client_a, w):
    client_a.force_login(w.a.user)
    r = client_a.get(reverse("comms:inbox_item", args=[w.da.pk]))
    html = r.content.decode()
    assert "&lt;b&gt;early&lt;/b&gt;" in html
    assert "<b>early</b>" not in html
    assert "<br>" in html


def test_other_students_delivery_is_404(client_a, w):
    client_a.force_login(w.a.user)
    assert client_a.get(reverse("comms:inbox_item", args=[w.db.pk])).status_code == 404
    w.db.refresh_from_db()
    assert w.db.read_at is None


@pytest.mark.parametrize("raw", ["0", "99999", "abc", "-1", "１２"])
def test_bad_ids_are_404(client_a, w, raw):
    client_a.force_login(w.a.user)
    assert client_a.get(f"/inbox/{raw}/").status_code == 404


def test_opening_marks_read_and_drops_unread(client_a, w):
    client_a.force_login(w.a.user)
    assert unread_count(w.a.user) == 1
    r = client_a.get(reverse("comms:inbox_item", args=[w.da.pk]))
    assert r.status_code == 200
    w.da.refresh_from_db()
    assert w.da.read_at is not None
    first = w.da.read_at
    assert unread_count(w.a.user) == 0
    client_a.get(reverse("comms:inbox_item", args=[w.da.pk]))
    w.da.refresh_from_db()
    assert w.da.read_at == first


def test_mark_all_read_is_post_only_with_csrf(client_a, school_a, w):
    from django_tenants.test.client import TenantClient

    client_a.force_login(w.a.user)
    assert client_a.get(reverse("comms:inbox_read_all")).status_code == 405
    strict = TenantClient(school_a, enforce_csrf_checks=True)
    strict.force_login(w.a.user)
    assert strict.post(reverse("comms:inbox_read_all")).status_code == 403
    assert unread_count(w.a.user) == 1
    r = client_a.post(reverse("comms:inbox_read_all"))
    assert r.status_code == 302
    assert unread_count(w.a.user) == 0
    assert unread_count(w.b.user) == 2  # untouched


def test_staff_gets_403(client_a, w):
    staff = StaffFactory.create()
    client_a.force_login(staff)
    assert client_a.get(reverse("comms:inbox")).status_code == 403
    assert client_a.get(reverse("comms:inbox_item", args=[w.da.pk])).status_code == 403
    assert client_a.post(reverse("comms:inbox_read_all")).status_code == 403


def test_anonymous_redirected(client_a, w):
    assert client_a.get(reverse("comms:inbox")).status_code == 302


def test_email_deliveries_are_not_in_inbox(client_a, w):
    Delivery.objects.filter(pk=w.da.pk).update(channel="email")
    client_a.force_login(w.a.user)
    assert "Sports day" not in client_a.get(reverse("comms:inbox")).content.decode()
    assert unread_count(w.a.user) == 0


def test_pagination_20(client_a, w):
    for i in range(24):
        service.notify(
            "announcement",
            students=[w.a],
            subject=f"N{i}",
            body="x",
            per_student=False,
            channels=("inapp",),
        )
    client_a.force_login(w.a.user)
    r = client_a.get(reverse("comms:inbox"))
    assert len(r.context["page"].object_list) == 20
    r = client_a.get(reverse("comms:inbox") + "?page=2")
    assert len(r.context["page"].object_list) == 5


def test_unread_count_single_query(w):
    user = w.b.user
    with CaptureQueriesContext(connection) as ctx:
        assert unread_count(user) == 2
    selects = [q for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1


def test_portal_badge(client_a, w):
    client_a.force_login(w.a.user)
    html = client_a.get(reverse("home")).content.decode()
    assert "<strong>Inbox (1)</strong>" in html
    Delivery.objects.filter(user=w.a.user).update(read_at=timezone.now())
    html = client_a.get(reverse("home")).content.decode()
    assert "Inbox (" not in html
    assert reverse("comms:inbox") in html
