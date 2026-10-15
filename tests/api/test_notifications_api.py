import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.comms import service
from apps.comms.models import Delivery
from tests.accounts.factories import StaffFactory
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/notifications"


@pytest.fixture
def w(school_a):
    from types import SimpleNamespace

    with schema_context("school_a"):
        a = StudentProfileFactory()
        b = StudentProfileFactory()
        m1 = service.notify(
            "announcement",
            students=[a, b],
            subject="One",
            body="Hello",
            per_student=False,
            channels=("inapp",),
        )
        m2 = service.notify(
            "announcement",
            students=[b],
            subject="Two",
            body="For B",
            per_student=False,
            channels=("inapp",),
        )
        yield SimpleNamespace(
            a=a,
            b=b,
            da=Delivery.objects.get(message=m1, student=a),
            db=Delivery.objects.get(message=m2, student=b),
        )


def test_url_names():
    assert reverse("api:notifications") == URL
    assert reverse("api:notification_read", args=[7]) == f"{URL}/7/read"


def test_list_shape_and_scope(client_a, w):
    client_a.force_login(w.a.user)
    r = client_a.get(URL)
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"count", "next", "previous", "results"}
    assert data["count"] == 1
    item = data["results"][0]
    assert set(item) == {"id", "subject", "body", "created_at", "read"}
    assert item["id"] == w.da.pk
    assert (item["subject"], item["body"], item["read"]) == ("One", "Hello", False)


def test_newest_first(client_a, w):
    client_a.force_login(w.b.user)
    ids = [i["id"] for i in client_a.get(URL).json()["results"]]
    assert ids == [w.db.pk, Delivery.objects.get(student=w.b, message__subject="One").pk]


def test_staff_403(client_a, w):
    client_a.force_login(StaffFactory.create())
    r = client_a.get(URL)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert client_a.post(f"{URL}/{w.da.pk}/read").status_code == 403


def test_anonymous_401_or_403(client_a, w):
    assert client_a.get(URL).status_code in (401, 403)
    assert client_a.post(f"{URL}/{w.da.pk}/read").status_code in (401, 403)


def test_read_marks_and_is_idempotent(client_a, w):
    client_a.force_login(w.a.user)
    url = f"{URL}/{w.da.pk}/read"
    r = client_a.post(url)
    assert r.status_code == 200
    assert r.json() == {"id": w.da.pk, "read": True}
    w.da.refresh_from_db()
    first = w.da.read_at
    assert first is not None
    assert client_a.post(url).json() == {"id": w.da.pk, "read": True}
    w.da.refresh_from_db()
    assert w.da.read_at == first
    assert client_a.get(URL).json()["results"][0]["read"] is True


def test_read_other_students_delivery_404(client_a, w):
    client_a.force_login(w.a.user)
    r = client_a.post(f"{URL}/{w.db.pk}/read")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    w.db.refresh_from_db()
    assert w.db.read_at is None


def test_read_is_post_only(client_a, w):
    client_a.force_login(w.a.user)
    assert client_a.get(f"{URL}/{w.da.pk}/read").status_code == 405
