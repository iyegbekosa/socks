"""Student fees portal (FR-15, UC-12): own invoices and receipts, siblings' balances only."""

from unittest import mock

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import Term
from apps.fees import hooks
from apps.fees.invoicing import outstanding_for
from apps.fees.models import InvoiceLine, Receipt
from apps.tenancy.models import School
from tests.accounts.factories import StudentFactory
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _confirm, _invoice, _world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

URL = "/fees/my/"
PHONE = "+2348039990001"


def _portal_world():
    w = _world()
    Term.objects.filter(pk=w.term1.pk).update(is_current=True)
    w.student.first_name, w.student.last_name = "Adaeze", "Okafor"
    w.student.guardian_phone = PHONE
    w.student.save()
    w.inv1 = _invoice(w.student, w.term1, 5_000_000)
    InvoiceLine.objects.create(
        invoice=w.inv1, kind=InvoiceLine.OPTIONAL, description="Ownbus Levy", amount_kobo=0
    )
    w.inv2 = _invoice(w.student, w.term2, 2_000_000)
    w.sibling = StudentProfileFactory(
        first_name="Chibuzo", last_name="Zzsiblastname", guardian_phone=PHONE
    )
    w.sib_inv = _invoice(w.sibling, w.term1, 3_000_000)
    InvoiceLine.objects.create(
        invoice=w.sib_inv, kind=InvoiceLine.OPTIONAL, description="Sibbus Levy", amount_kobo=0
    )
    w.stranger = StudentProfileFactory(first_name="Strangerfirst")
    w.stranger_inv = _invoice(w.stranger, w.term1, 1_000_000)
    with mock.patch.object(hooks, "on_payment_confirmed"):
        w.payment = _confirm(w, [(w.inv1, 1_000_000)], 1_000_000)
        sib_w = type(w)(student=w.sibling, bursar=w.bursar)
        w.sib_payment = _confirm(sib_w, [(w.sib_inv, 500_000)], 500_000)
    w.receipt = Receipt.objects.get(payment=w.payment)
    w.sib_receipt = Receipt.objects.get(payment=w.sib_payment)
    return w


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield _portal_world()


def test_url_name():
    assert reverse("fees:my_fees") == URL


def test_student_sees_own_invoices_lines_and_receipts(w, client_a):
    client_a.force_login(w.student.user)
    r = client_a.get(URL)
    assert r.status_code == 200
    assert "no-store" in r.headers.get("Cache-Control", "") or "max-age=0" in r.headers.get(
        "Cache-Control", ""
    )
    html = r.content.decode()
    assert "Tuition" in html and "Ownbus Levy" in html
    assert "₦50,000.00" in html  # inv1 total
    assert "₦40,000.00" in html  # inv1 balance after ₦10,000 paid
    assert "₦10,000.00" in html  # inv1 paid
    assert "Part paid" in html and "Unpaid" in html
    assert str(w.term1) in html and str(w.term2) in html
    # Newest first: term 2 before term 1.
    assert html.index(str(w.term2)) < html.index(str(w.term1))
    assert reverse("fees:pay") in html and "Pay now" in html
    assert w.receipt.number in html
    assert reverse("fees:receipt_download", args=[w.receipt.pk]) in html


def test_siblings_show_first_name_class_and_balance_only(w, client_a):
    client_a.force_login(w.student.user)
    html = client_a.get(URL).content.decode()
    assert "Chibuzo" in html
    assert "JSS 1A" in html
    with schema_context("school_a"):
        assert outstanding_for(w.sibling) == 2_500_000
    assert "₦25,000.00" in html
    # Never the sibling's last name, invoice lines, receipts or payments.
    assert "Zzsiblastname" not in html
    assert "Sibbus Levy" not in html
    assert w.sib_receipt.number not in html
    assert reverse("fees:receipt_download", args=[w.sib_receipt.pk]) not in html
    # A stranger is not a sibling.
    assert "Strangerfirst" not in html


def test_pay_for_siblings_link_preselects_them(w, client_a):
    with schema_context("school_a"):
        School.objects.filter(schema_name="school_a").update(paystack_subaccount_code="ACCT_x")
    client_a.force_login(w.student.user)
    html = client_a.get(URL).content.decode()
    assert "Pay for siblings too" in html
    link = f"{reverse('fees:pay')}?with={w.sibling.pk}"
    assert link in html
    pay = client_a.get(link)
    assert pay.status_code == 200
    page = pay.content.decode()
    assert f'value="{w.sib_inv.pk}" id="pay_{w.sib_inv.pk}" checked' in page
    assert f'value="{w.inv1.pk}" id="pay_{w.inv1.pk}" checked' in page
    # Preselecting a stranger selects nothing of theirs (it is not even listed).
    page = client_a.get(f"{reverse('fees:pay')}?with={w.stranger.pk}").content.decode()
    assert f'value="{w.stranger_inv.pk}"' not in page
    assert f'value="{w.sib_inv.pk}" id="pay_{w.sib_inv.pk}" checked' not in page
    assert client_a.get(f"{reverse('fees:pay')}?with=abc").status_code == 404


def test_no_pay_now_when_nothing_owed(w, client_a):
    with schema_context("school_a"):
        from apps.fees.models import Invoice

        for inv in Invoice.objects.filter(student=w.student):
            Invoice.objects.filter(pk=inv.pk).update(paid_kobo=inv.total_kobo, status="paid")
        Invoice.objects.filter(student=w.sibling).update(status="carried")
    client_a.force_login(w.student.user)
    html = client_a.get(URL).content.decode()
    assert "Pay now" not in html and "Pay for siblings too" not in html


@pytest.mark.parametrize("role", ["bursar", "teacher", "proprietor"])
def test_staff_are_forbidden(w, client_a, role):
    from tests.accounts.helpers import login_verified

    with schema_context("school_a"):
        user = make_staff(role)
    login_verified(client_a, user)
    assert client_a.get(URL).status_code == 403


def test_student_without_profile_is_404(school_a, client_a):
    with schema_context("school_a"):
        user = StudentFactory()
        client_a.force_login(user)
    assert client_a.get(URL).status_code == 404


def test_anonymous_redirects_to_login(school_a, client_a):
    r = client_a.get(URL)
    assert r.status_code == 302 and "login" in r["Location"]


def test_post_not_allowed(w, client_a):
    client_a.force_login(w.student.user)
    assert client_a.post(URL).status_code == 405


def test_portal_home_shows_balance_and_fees_link(w, client_a):
    client_a.force_login(w.student.user)
    html = client_a.get(reverse("home")).content.decode()
    assert reverse("fees:my_fees") in html and ">Fees<" in html
    assert "₦60,000.00" in html  # 40,000 + 20,000 outstanding
    assert html.count(f'href="{reverse("fees:pay")}"') <= 1
