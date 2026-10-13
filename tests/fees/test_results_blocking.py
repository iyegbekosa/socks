"""Results withheld while fees are owed (UC-17)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import cloudinary
import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import Term
from apps.fees import hooks, invoicing
from apps.fees.blocking import WITHHELD_MESSAGE, owed_up_to, results_withheld
from apps.fees.models import FeeSettings, Invoice
from apps.results.models import ResultSummary
from apps.students.models import Student
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_item, make_staff, make_structure
from tests.fees.test_ledger import _confirm, _invoice, _world
from tests.results.factories import make_card, make_sheet_world

pytestmark = pytest.mark.django_db

MESSAGE = (
    "Your results for this term are withheld until outstanding fees are paid. "
    "Please contact the school bursar."
)


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


def _block(on: bool = True) -> None:
    FeeSettings.objects.update_or_create(pk=1, defaults={"block_results_when_owing": on})


def _pay(student, bursar, invoice, amount):
    with mock.patch.object(hooks, "on_payment_confirmed"):
        _confirm(SimpleNamespace(student=student, bursar=bursar), [(invoice, amount)], amount)


@pytest.fixture
def r(school_a):
    with schema_context("school_a"):
        w = make_sheet_world()
        st = w.students[0]
        make_card(st, w.term, w.arm)
        make_card(st, w.term2, w.arm)
        ResultSummary.objects.filter(student=st, term=w.term2).update(
            total=Decimal("654.3"), average=Decimal("65.43")
        )
        bursar = make_staff("bursar")
        inv = _invoice(st, w.term, 100)  # ₦1
        yield SimpleNamespace(w=w, st=st, inv=inv, bursar=bursar)


def test_message_is_exact():
    assert WITHHELD_MESSAGE == MESSAGE


def card_url(r, term) -> str:
    return reverse("results:report_card", args=[r.st.pk, term.pk])


def api_url(r, term) -> str:
    return reverse("api:report_card", args=[r.st.pk, term.pk])


def test_student_owing_one_naira_is_blocked(client_a, r):
    with schema_context("school_a"):
        _block()
    client_a.force_login(r.st.user)
    resp = client_a.get(reverse("results:my_results"))
    html = resp.content.decode()
    assert resp.status_code == 200
    # Term 1 and term 2 both include the ₦1 owed in term 1.
    assert html.count(MESSAGE) == 2
    assert "654.3" not in html and "50.00" not in html
    assert card_url(r, r.w.term) not in html

    resp = client_a.get(card_url(r, r.w.term))
    assert resp.status_code == 403
    assert MESSAGE in resp.content.decode()

    resp = client_a.get(api_url(r, r.w.term))
    assert resp.status_code == 403
    error = resp.json()["error"]
    assert (error["code"], error["message"]) == ("RESULTS_WITHHELD", MESSAGE)


def test_only_withheld_terms_are_hidden(client_a, r):
    with schema_context("school_a"):
        _block()
        _pay(r.st, r.bursar, r.inv, 100)
        _invoice(r.st, r.w.term2, 500_000)
    client_a.force_login(r.st.user)
    html = client_a.get(reverse("results:my_results")).content.decode()
    assert html.count(MESSAGE) == 1
    assert "654.3" not in html
    assert "50.00" in html and card_url(r, r.w.term) in html
    assert client_a.get(card_url(r, r.w.term)).status_code == 302
    assert client_a.get(card_url(r, r.w.term2)).status_code == 403


def test_after_paying_results_show(client_a, r):
    with schema_context("school_a"):
        _block()
        _pay(r.st, r.bursar, r.inv, 100)
    client_a.force_login(r.st.user)
    html = client_a.get(reverse("results:my_results")).content.decode()
    assert MESSAGE not in html
    assert "654.3" in html and "50.00" in html
    assert client_a.get(card_url(r, r.w.term)).status_code == 302
    assert client_a.get(api_url(r, r.w.term)).status_code == 302


def test_blocking_off_debtor_sees_results(client_a, r):
    with schema_context("school_a"):
        _block(False)
    client_a.force_login(r.st.user)
    html = client_a.get(reverse("results:my_results")).content.decode()
    assert MESSAGE not in html and "654.3" in html
    assert client_a.get(card_url(r, r.w.term)).status_code == 302
    assert client_a.get(api_url(r, r.w.term)).status_code == 302


def test_no_settings_row_means_not_blocked(r):
    with schema_context("school_a"):
        FeeSettings.objects.all().delete()
        assert results_withheld(r.st, r.w.term) is False
        assert not FeeSettings.objects.exists()


def test_staff_are_unaffected(client_a, r):
    with schema_context("school_a"):
        _block()
        principal = make_staff("principal")
    login_verified(client_a, principal)
    with mock.patch("apps.fees.blocking.results_withheld") as withheld:
        assert client_a.get(card_url(r, r.w.term)).status_code == 302
        assert client_a.get(api_url(r, r.w.term)).status_code == 302
    withheld.assert_not_called()


def test_staff_linked_to_a_student_is_unaffected(client_a, r):
    """Blocking is by user kind: a staff login attached to a student row is still staff."""
    with schema_context("school_a"):
        _block()
        principal = make_staff("principal")
        Student.objects.filter(pk=r.st.pk).update(user=principal)
    login_verified(client_a, principal)
    assert client_a.get(card_url(r, r.w.term)).status_code == 302
    assert client_a.get(api_url(r, r.w.term)).status_code == 302


# --- debt attribution across carried balances (ruling G) ----------------------------------


def _chain_world():
    w = _world()
    w.term3, _ = Term.objects.get_or_create(
        session=w.session,
        number=3,
        defaults={"start_date": date(2027, 4, 20), "end_date": date(2027, 7, 20)},
    )
    item = make_item()
    make_structure(w.level, w.term1, [(item, 1_000_000, False)])
    make_structure(w.level, w.term2, [(item, 2_000_000, False)])
    make_structure(w.level, w.term3, [(item, 500_000, False)])
    _block()
    return w


def test_carried_debt_still_withholds_the_earlier_term(school_a):
    with schema_context("school_a"):
        w = _chain_world()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        invoicing.generate_invoices(w.term2, by=w.bursar)
        inv1 = Invoice.objects.get(student=w.student, term=w.term1)
        inv2 = Invoice.objects.get(student=w.student, term=w.term2)
        assert inv1.status == Invoice.CARRIED and inv2.total_kobo == 3_000_000
        _pay(w.student, w.bursar, inv2, 2_000_000)  # the new fees, not the ₦10,000 carried
        assert invoicing.outstanding_for(w.student, up_to_term=w.term1) == 0
        assert owed_up_to(w.student, w.term1) == 1_000_000
        assert results_withheld(w.student, w.term1) is True
        assert results_withheld(w.student, w.term2) is True

        # Term 3 carries term 2's remaining ₦10,000 forward again.
        invoicing.generate_invoices(w.term3, by=w.bursar)
        inv3 = Invoice.objects.get(student=w.student, term=w.term3)
        assert inv3.total_kobo == 1_500_000
        assert results_withheld(w.student, w.term1) is True
        _pay(w.student, w.bursar, inv3, 500_000)
        assert results_withheld(w.student, w.term1) is True
        _pay(w.student, w.bursar, inv3, 1_000_000)
        assert owed_up_to(w.student, w.term1) == 0
        for term in (w.term1, w.term2, w.term3):
            assert results_withheld(w.student, term) is False


def test_new_fees_only_do_not_withhold_the_earlier_term(school_a):
    with schema_context("school_a"):
        w = _chain_world()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        inv1 = Invoice.objects.get(student=w.student, term=w.term1)
        _pay(w.student, w.bursar, inv1, 1_000_000)
        invoicing.generate_invoices(w.term2, by=w.bursar)
        inv2 = Invoice.objects.get(student=w.student, term=w.term2)
        assert inv2.total_kobo == 2_000_000  # nothing brought forward
        assert results_withheld(w.student, w.term1) is False
        assert results_withheld(w.student, w.term2) is True


def test_payment_of_full_balance_releases_the_carried_term(school_a):
    with schema_context("school_a"):
        w = _chain_world()
        invoicing.generate_invoices(w.term1, by=w.bursar)
        invoicing.generate_invoices(w.term2, by=w.bursar)
        inv2 = Invoice.objects.get(student=w.student, term=w.term2)
        _pay(w.student, w.bursar, inv2, 2_500_000)  # half of the carried ₦10,000 is still owed
        assert owed_up_to(w.student, w.term1) == 500_000
        _pay(w.student, w.bursar, inv2, 500_000)
        assert results_withheld(w.student, w.term1) is False
