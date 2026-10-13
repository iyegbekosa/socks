import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel
from apps.audit.models import AuditEvent
from apps.fees import invoicing
from apps.fees.models import Invoice, InvoiceLine
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_item, make_staff, make_structure, make_terms
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def _login(client, role):
    with schema_context("school_a"):
        user = make_staff(role)
    if role in ("school_admin", "principal", "bursar"):
        login_verified(client, user)
    else:
        with schema_context("school_a"):
            client.force_login(user)
    return user


def _world(schema="school_a"):
    with schema_context(schema):
        w = make_terms()
        w.tuition, w.bus = make_item("Tuition"), make_item("Bus")
        make_structure(w.level, w.term1, [(w.tuition, 5_500_000, False), (w.bus, 2_000_000, True)])
        w.student = StudentProfileFactory()
        return w


def _generate(w, schema="school_a"):
    with schema_context(schema):
        invoicing.generate_invoices(w.term1, by=make_staff("bursar"))
        return Invoice.objects.get(student=w.student, term=w.term1)


def test_teacher_gets_403_everywhere(client_a):
    w = _world()
    inv = _generate(w)
    _login(client_a, "teacher")
    for url in (
        reverse("fees:invoices"),
        reverse("fees:invoice", args=[inv.pk]),
        reverse("fees:invoice", args=[999999]),
        reverse("fees:invoice", args=["abc"]),
    ):
        assert client_a.get(url).status_code == 403, url
        assert client_a.post(url, {}).status_code == 403, url


def test_bad_or_foreign_invoice_is_404(client_a, school_b):
    _world()
    wb = _world("school_b")
    foreign = _generate(wb, "school_b")
    _login(client_a, "bursar")
    with schema_context("school_a"):
        assert not Invoice.objects.filter(pk=foreign.pk).exists()
    for raw in (str(foreign.pk), "abc", "1x", "0" * 19):
        url = reverse("fees:invoice", args=[raw])
        assert client_a.get(url).status_code == 404, raw
    assert client_a.get(reverse("fees:invoices") + "?term=abc").status_code == 404
    assert client_a.get(reverse("fees:invoices") + "?term=999999").status_code == 404


def test_bursar_generates_from_list_page(client_a):
    w = _world()
    _login(client_a, "bursar")
    url = reverse("fees:invoices") + f"?term={w.term1.pk}"
    assert client_a.get(url).status_code == 200
    r = client_a.post(reverse("fees:invoices"), {"generate": "1", "term": w.term1.pk})
    assert r.status_code == 302
    with schema_context("school_a"):
        inv = Invoice.objects.get(student=w.student, term=w.term1)
        assert inv.total_kobo == 5_500_000
        assert AuditEvent.objects.filter(action="fees.invoices_generated").count() == 1
    page = client_a.get(url, follow=True)
    body = page.content.decode()
    assert "Created 1 invoice" in body
    assert "0 student(s) already have an invoice for a later term" in body
    assert "₦55,000.00" in body
    assert w.student.last_name in body
    # filters
    r = client_a.get(url + f"&level={w.level.pk}")
    assert w.student.last_name in r.content.decode()
    with schema_context("school_a"):
        other = ClassLevel.objects.create(name="SS 1", section="ss", order=12)
        other_arm = ClassArm.objects.create(level=other, suffix="A")
    r = client_a.get(url + f"&level={other.pk}")
    assert w.student.last_name not in r.content.decode()
    r = client_a.get(url + f"&arm={other_arm.pk}")
    assert w.student.last_name not in r.content.decode()
    assert client_a.get(url + "&level=bad").status_code == 404


def test_proprietor_can_view_but_not_generate_or_adjust(client_a):
    w = _world()
    inv = _generate(w)
    _login(client_a, "proprietor")
    assert client_a.get(reverse("fees:invoices")).status_code == 200
    assert client_a.get(reverse("fees:invoice", args=[inv.pk])).status_code == 200
    assert (
        client_a.post(reverse("fees:invoices"), {"generate": "1", "term": w.term1.pk}).status_code
        == 403
    )
    r = client_a.post(
        reverse("fees:invoice", args=[inv.pk]),
        {"action": "adjustment", "amount": "-3000", "reason": "x"},
    )
    assert r.status_code == 403


def test_bursar_adds_adjustment(client_a):
    w = _world()
    inv = _generate(w)
    _login(client_a, "bursar")
    url = reverse("fees:invoice", args=[inv.pk])
    r = client_a.get(url)
    assert r.status_code == 200
    assert "Tuition" in r.content.decode()
    r = client_a.post(
        url, {"action": "adjustment", "amount": "-3,000", "reason": "Agreed sibling waiver"}
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        inv.refresh_from_db()
        assert inv.total_kobo == 5_200_000
        assert inv.status == Invoice.UNPAID
        line = inv.lines.get(kind=InvoiceLine.ADJUSTMENT)
        assert line.amount_kobo == -300_000 and line.reason == "Agreed sibling waiver"
        event = AuditEvent.objects.get(action="fees.invoice_adjusted")
        assert event.after["line_id"] == line.pk
        assert "sibling" not in str(event.after).lower()


def test_adjustment_requires_reason_in_form(client_a):
    w = _world()
    inv = _generate(w)
    _login(client_a, "bursar")
    r = client_a.post(
        reverse("fees:invoice", args=[inv.pk]),
        {"action": "adjustment", "amount": "-3000", "reason": ""},
    )
    assert r.status_code == 200
    with schema_context("school_a"):
        assert not InvoiceLine.objects.filter(kind=InvoiceLine.ADJUSTMENT).exists()


def test_adjustment_below_paid_is_form_error(client_a):
    w = _world()
    inv = _generate(w)
    with schema_context("school_a"):
        Invoice.objects.filter(pk=inv.pk).update(paid_kobo=5_000_000, status=Invoice.PART_PAID)
    _login(client_a, "bursar")
    r = client_a.post(
        reverse("fees:invoice", args=[inv.pk]),
        {"action": "adjustment", "amount": "-6000", "reason": "Too generous"},
    )
    assert r.status_code == 200
    assert "below the amount already paid" in r.content.decode()
    with schema_context("school_a"):
        inv.refresh_from_db()
        assert inv.total_kobo == 5_500_000


def test_adjustment_on_carried_invoice_is_409(client_a):
    w = _world()
    inv = _generate(w)
    with schema_context("school_a"):
        Invoice.objects.filter(pk=inv.pk).update(status=Invoice.CARRIED)
    _login(client_a, "bursar")
    r = client_a.post(
        reverse("fees:invoice", args=[inv.pk]),
        {"action": "adjustment", "amount": "-3000", "reason": "x"},
    )
    assert r.status_code == 409
    with schema_context("school_a"):
        assert not InvoiceLine.objects.filter(kind=InvoiceLine.ADJUSTMENT).exists()


def test_bursar_adds_optional_item_once(client_a):
    w = _world()
    inv = _generate(w)
    _login(client_a, "bursar")
    url = reverse("fees:invoice", args=[inv.pk])
    with schema_context("school_a"):
        bus_line = w.level.fee_structures.get(term=w.term1).lines.get(item=w.bus)
    assert "Bus" in client_a.get(url).content.decode()
    r = client_a.post(url, {"action": "optional", "line": bus_line.pk})
    assert r.status_code == 302
    with schema_context("school_a"):
        inv.refresh_from_db()
        assert inv.total_kobo == 7_500_000
        assert inv.lines.get(kind=InvoiceLine.OPTIONAL).item_id == w.bus.pk
    r = client_a.post(url, {"action": "optional", "line": bus_line.pk})
    assert r.status_code == 200  # no longer a valid choice
    with schema_context("school_a"):
        assert inv.lines.filter(kind=InvoiceLine.OPTIONAL).count() == 1
