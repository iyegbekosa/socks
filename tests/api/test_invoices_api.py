"""GET /api/v1/invoices (§6.2) and the student balance in /api/v1/me."""

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.fees.invoicing import outstanding_for
from apps.fees.models import Invoice
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _invoice, _world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/invoices"
PHONE = "+2348039990002"


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        w = _world()
        w.student.guardian_phone = PHONE
        w.student.save()
        w.inv1 = _invoice(w.student, w.term1, 5_000_000, paid_kobo=1_000_000)
        w.inv2 = _invoice(w.student, w.term2, 2_000_000)
        w.sibling = StudentProfileFactory(first_name="Chibuzo", guardian_phone=PHONE)
        w.sib_inv = _invoice(w.sibling, w.term1, 3_000_000)
        w.other = StudentProfileFactory()
        w.other_inv = _invoice(w.other, w.term1, 4_000_000)
        yield w


def _items(r):
    assert r.status_code == 200, r.content
    data = r.json()
    assert set(data) == {"count", "next", "previous", "results"}
    return data["results"]


def test_url_name():
    assert reverse("api:invoices") == URL


def test_student_lists_own_and_sibling_invoices(w, client_a):
    client_a.force_login(w.student.user)
    items = _items(client_a.get(URL))
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {w.inv1.pk, w.inv2.pk, w.sib_inv.pk}
    own = by_id[w.inv1.pk]
    assert set(own) == {"id", "student", "term", "status", "total", "paid", "balance", "lines"}
    assert own["student"] == {
        "id": w.student.pk,
        "admission_number": w.student.admission_number,
        "first_name": w.student.first_name,
    }
    assert own["term"] == {"id": w.term1.pk, "name": "1st Term", "session": "2026/2027"}
    assert own["status"] == "part_paid"
    assert own["total"] == {"amount_kobo": 5_000_000, "amount_display": "₦50,000.00"}
    assert own["paid"] == {"amount_kobo": 1_000_000, "amount_display": "₦10,000.00"}
    assert own["balance"] == {"amount_kobo": 4_000_000, "amount_display": "₦40,000.00"}
    assert own["lines"] == [
        {
            "kind": "fee",
            "description": "Tuition",
            "amount": {"amount_kobo": 5_000_000, "amount_display": "₦50,000.00"},
        }
    ]
    sib = by_id[w.sib_inv.pk]
    assert "lines" not in sib
    assert sib["student"]["first_name"] == "Chibuzo"
    assert "last_name" not in sib["student"]


def test_student_filters_self_and_sibling(w, client_a):
    client_a.force_login(w.student.user)
    items = _items(client_a.get(URL, {"student": w.student.pk}))
    assert {i["id"] for i in items} == {w.inv1.pk, w.inv2.pk}
    items = _items(client_a.get(URL, {"student": w.sibling.pk}))
    assert [i["id"] for i in items] == [w.sib_inv.pk] and "lines" not in items[0]


@pytest.mark.parametrize("raw", ["other", "999999", "abc", "-1"])
def test_student_filtering_a_non_sibling_is_404(w, client_a, raw):
    client_a.force_login(w.student.user)
    value = str(w.other.pk) if raw == "other" else raw
    r = client_a.get(URL, {"student": value})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert str(w.other_inv.pk) not in r.content.decode()


def test_term_filter(w, client_a):
    client_a.force_login(w.student.user)
    items = _items(client_a.get(URL, {"term": w.term2.pk}))
    assert [i["id"] for i in items] == [w.inv2.pk]
    assert client_a.get(URL, {"term": "x"}).status_code == 404
    assert client_a.get(URL, {"term": "999999"}).status_code == 404


def test_carried_invoices_are_included_with_status(w, client_a):
    with schema_context("school_a"):
        Invoice.objects.filter(pk=w.inv1.pk).update(status=Invoice.CARRIED)
    client_a.force_login(w.student.user)
    items = {i["id"]: i for i in _items(client_a.get(URL))}
    assert items[w.inv1.pk]["status"] == "carried"


def test_bursar_needs_student(w, client_a):
    login_verified(client_a, w.bursar)
    r = client_a.get(URL)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert {d["field"] for d in err["details"]} == {"student"}


def test_bursar_reads_any_student_with_lines(w, client_a):
    login_verified(client_a, w.bursar)
    items = _items(client_a.get(URL, {"student": w.other.pk}))
    assert [i["id"] for i in items] == [w.other_inv.pk]
    assert items[0]["lines"][0]["description"] == "Tuition"
    assert client_a.get(URL, {"student": "999999"}).status_code == 404
    assert client_a.get(URL, {"student": "x"}).status_code == 404


def test_proprietor_may_read(w, client_a):
    with schema_context("school_a"):
        user = make_staff("proprietor")
    client_a.force_login(user)
    assert _items(client_a.get(URL, {"student": w.student.pk}))


@pytest.mark.parametrize("role", ["teacher", "principal"])
def test_other_staff_forbidden(w, client_a, role):
    with schema_context("school_a"):
        user = make_staff(role)
    login_verified(client_a, user)
    r = client_a.get(URL, {"student": w.student.pk})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_anonymous_401(school_a, client_a):
    assert client_a.get(URL).status_code == 401


def test_pagination(w, client_a):
    client_a.force_login(w.student.user)
    data = client_a.get(URL, {"page_size": 1}).json()
    assert data["count"] == 3 and len(data["results"]) == 1 and data["next"]


def test_post_not_allowed(w, client_a):
    client_a.force_login(w.student.user)
    assert client_a.post(URL).status_code == 405


def test_me_balance_equals_outstanding(w, client_a):
    client_a.force_login(w.student.user)
    data = client_a.get("/api/v1/me").json()
    with schema_context("school_a"):
        expected = outstanding_for(w.student)
    assert expected == 6_000_000
    assert data["balance"] == {"amount_kobo": expected, "amount_display": "₦60,000.00"}


def test_staff_me_has_no_balance(school_a, client_a):
    with schema_context("school_a"):
        user = StaffFactory()
        client_a.force_login(user)
    assert "balance" not in client_a.get("/api/v1/me").json()
