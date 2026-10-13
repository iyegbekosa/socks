"""Debtors list, CSV export and the fees dashboard (UC-14)."""

import csv
import io
from datetime import UTC, datetime
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel
from apps.fees import debtors, hooks, invoicing
from apps.fees.models import Invoice, ManualPayment, Payment, PaymentAttempt
from apps.students.models import Student
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_item, make_staff, make_structure
from tests.fees.test_ledger import _confirm, _invoice, _world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

URL = "/fees/debtors/"
DASHBOARD = "/fees/"


def _debtor_world():
    w = _world()
    w.big = StudentProfileFactory(user__admission_number="GFS/2026/0901")
    w.small = StudentProfileFactory(user__admission_number="GFS/2026/0902")
    w.clear = StudentProfileFactory(user__admission_number="GFS/2026/0903")
    w.gone = StudentProfileFactory(user__admission_number="GFS/2026/0904")
    Student.objects.filter(pk=w.gone.pk).update(status=Student.Status.WITHDRAWN)
    w.big_inv = _invoice(w.big, w.term1, 5_000_000)
    w.small_inv = _invoice(w.small, w.term1, 1_500_000)
    w.clear_inv = _invoice(w.clear, w.term1, 2_000_000)
    w.gone_inv = _invoice(w.gone, w.term1, 500_000)
    with mock.patch.object(hooks, "on_payment_confirmed"):
        w.small_pay = _confirm(
            type(w)(student=w.small, bursar=w.bursar),
            [(w.small_inv, 500_000)],
            500_000,
            paid_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        )
        _confirm(
            type(w)(student=w.clear, bursar=w.bursar),
            [(w.clear_inv, 2_000_000)],
            2_000_000,
        )
    return w


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield _debtor_world()


def test_url_names():
    assert reverse("fees:debtors") == URL
    assert reverse("fees:dashboard") == DASHBOARD
    assert reverse("fees:structures") == "/fees/structures/"


def test_debtors_ordered_by_balance_with_withdrawn_marked(w):
    with schema_context("school_a"):
        rows = debtors.debtors(w.term1)
    assert [r.student.pk for r in rows] == [w.big.pk, w.small.pk, w.gone.pk]
    assert [r.balance_kobo for r in rows] == [5_000_000, 1_000_000, 500_000]
    assert [r.withdrawn for r in rows] == [False, False, True]
    small = rows[1]
    assert (small.total_kobo, small.paid_kobo) == (1_500_000, 500_000)
    assert small.last_payment_at == w.small_pay.paid_at
    assert rows[0].last_payment_at is None
    assert rows[0].arm is not None and rows[0].arm.pk == w.big.enrolments.get().arm_id


def test_minimum_balance_filter(w):
    with schema_context("school_a"):
        rows = debtors.debtors(w.term1, min_balance_kobo=2_000_000)
    assert [r.student.pk for r in rows] == [w.big.pk]


def test_ties_order_by_admission_number(school_a):
    with schema_context("school_a"):
        w = _world()
        b = StudentProfileFactory(user__admission_number="GFS/2026/0802")
        a = StudentProfileFactory(user__admission_number="GFS/2026/0801")
        _invoice(b, w.term1, 100_000)
        _invoice(a, w.term1, 100_000)
        assert [r.student.pk for r in debtors.debtors(w.term1)] == [a.pk, b.pk]


def test_level_and_arm_filters(w):
    with schema_context("school_a"):
        level = ClassLevel.objects.create(name="JSS 3", section="jss", order=11)
        arm = ClassArm.objects.create(level=level, suffix="C")
        other = StudentProfileFactory(enrolment__arm=arm)
        _invoice(other, w.term1, 300_000)
        assert [r.student.pk for r in debtors.debtors(w.term1, level=level)] == [other.pk]
        assert [r.student.pk for r in debtors.debtors(w.term1, arm=arm)] == [other.pk]
        by_level = debtors.debtors(w.term1, level=w.level)
        assert other.pk not in {r.student.pk for r in by_level}
        assert len(by_level) == 3


def test_carried_balance_is_counted_once(school_a):
    with schema_context("school_a"):
        w = _world()
        item = make_item()
        make_structure(w.level, w.term1, [(item, 1_000_000, False)])
        make_structure(w.level, w.term2, [(item, 2_000_000, False)])
        invoicing.generate_invoices(w.term1, by=w.bursar)
        invoicing.generate_invoices(w.term2, by=w.bursar)
        assert Invoice.objects.get(student=w.student, term=w.term1).status == Invoice.CARRIED
        [row] = debtors.debtors(w.term2)
        assert row.balance_kobo == 3_000_000
        assert row.total_kobo == 3_000_000
        # Term 1's own list: its only invoice was carried, so nothing is owed "as of" it.
        assert debtors.debtors(w.term1) == []


def test_later_terms_are_not_counted(w):
    with schema_context("school_a"):
        _invoice(w.big, w.term2, 7_000_000)
        [first, *_] = debtors.debtors(w.term1)
        assert first.balance_kobo == 5_000_000
        assert debtors.debtors(w.term2)[0].balance_kobo == 12_000_000


def test_query_count_is_bounded(school_a):
    with schema_context("school_a"):
        w = _world()
        bursar = w.bursar
        for _ in range(30):
            st = StudentProfileFactory()
            inv = _invoice(st, w.term1, 400_000)
            with mock.patch.object(hooks, "on_payment_confirmed"):
                _confirm(type(w)(student=st, bursar=bursar), [(inv, 100_000)], 100_000)
        with CaptureQueriesContext(connection) as ctx:
            rows = debtors.debtors(w.term1)
            for r in rows:
                _ = (r.student.admission_number, r.student.full_name, str(r.arm or ""))
        assert len(rows) == 30
        # django-tenants re-sets search_path before each query; count the real ones.
        real = [q for q in ctx.captured_queries if not q["sql"].startswith("SET search_path")]
        assert len(real) <= 6


# --- the page and CSV ---------------------------------------------------------------------


def _login_bursar(client, w):
    login_verified(client, w.bursar)


def test_page_lists_debtors_and_filters_by_minimum(w, client_a):
    _login_bursar(client_a, w)
    r = client_a.get(URL, {"term": w.term1.pk})
    assert r.status_code == 200
    html = r.content.decode()
    assert w.big.full_name in html and w.small.full_name in html and w.gone.full_name in html
    assert w.clear.full_name not in html
    assert "Withdrawn" in html
    assert "₦50,000.00" in html
    r = client_a.get(URL, {"term": w.term1.pk, "min": "20000"})
    html = r.content.decode()
    assert w.big.full_name in html and w.small.full_name not in html


def test_bad_filters(w, client_a):
    _login_bursar(client_a, w)
    assert client_a.get(URL, {"term": "x"}).status_code == 404
    assert client_a.get(URL, {"term": w.term1.pk, "arm": "99999"}).status_code == 404
    r = client_a.get(URL, {"term": w.term1.pk, "min": "abc"})
    assert r.status_code == 200
    assert "Enter a valid amount" in r.content.decode()


def test_csv_export_guards_formulas(w, client_a):
    with schema_context("school_a"):
        Student.objects.filter(pk=w.big.pk).update(first_name="=cmd", middle_name="", last_name="")
    _login_bursar(client_a, w)
    r = client_a.get(URL, {"term": w.term1.pk, "export": "csv"})
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/csv")
    assert r["Content-Disposition"] == f'attachment; filename="debtors-{w.term1.pk}.csv"'
    assert r["Cache-Control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0] == [
        "Admission no.",
        "Name",
        "Class",
        "Total",
        "Paid",
        "Balance",
        "Last payment",
    ]
    assert rows[1][:2] == ["GFS/2026/0901", "'=cmd"]
    assert rows[1][3:6] == ["50000.00", "0.00", "50000.00"]
    assert rows[1][6] == ""
    assert rows[2][3:6] == ["15000.00", "5000.00", "10000.00"]
    assert rows[2][6] == "2026-10-01 10:00"
    assert len(rows) == 4


def test_proprietor_reads_teacher_forbidden(w, client_a):
    with schema_context("school_a"):
        proprietor = make_staff("proprietor")
        teacher = make_staff("teacher")
    client_a.force_login(proprietor)
    assert client_a.get(URL).status_code == 200
    assert client_a.get(DASHBOARD).status_code == 200
    client_a.force_login(teacher)
    assert client_a.get(URL).status_code == 403
    assert client_a.get(DASHBOARD).status_code == 403
    client_a.force_login(w.big.user)
    assert client_a.get(URL).status_code == 403


# --- dashboard ----------------------------------------------------------------------------


def test_dashboard_totals(w, client_a):
    today = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)  # 11:00 in Lagos
    with schema_context("school_a"):
        with mock.patch.object(hooks, "on_payment_confirmed"):
            today_pay = _confirm(
                type(w)(student=w.big, bursar=w.bursar),
                [(w.big_inv, 700_000)],
                700_000,
                paid_at=datetime(2026, 10, 12, 23, 30, tzinfo=UTC),  # 00:30 on the 13th, Lagos
            )
            _confirm(  # 23:59 on the 12th in Lagos: not today
                type(w)(student=w.gone, bursar=w.bursar),
                [(w.gone_inv, 100_000)],
                100_000,
                paid_at=datetime(2026, 10, 12, 22, 59, tzinfo=UTC),
            )
        other_term = _invoice(w.big, w.term2, 9_000_000)
        assert other_term.pk
        ManualPayment.objects.create(
            student=w.small,
            method="cash",
            amount_kobo=6_000_000,
            plan=[],
            paid_on=today.date(),
            recorded_by=w.bursar,
        )
        PaymentAttempt.objects.create(
            reference="B2L-FLAGGED0000000000000",
            student=w.small,
            amount_kobo=100,
            status=PaymentAttempt.FLAGGED,
        )
        Payment.objects.filter(pk=today_pay.pk).update(flagged=True)

        invoices = Invoice.objects.filter(term=w.term1)
        invoiced = sum(i.total_kobo for i in invoices)
        collected = sum(i.paid_kobo for i in invoices)
        outstanding = sum(i.balance_kobo for i in invoices.exclude(status=Invoice.CARRIED))
    login_verified(client_a, w.bursar)
    with mock.patch("django.utils.timezone.now", return_value=today):
        r = client_a.get(DASHBOARD, {"term": w.term1.pk})
    assert r.status_code == 200
    ctx = r.context
    assert ctx["invoiced_kobo"] == invoiced == 5_000_000 + 1_500_000 + 2_000_000 + 500_000
    assert ctx["collected_kobo"] == collected == 500_000 + 2_000_000 + 700_000 + 100_000
    assert ctx["outstanding_kobo"] == outstanding == invoiced - collected
    assert ctx["today_kobo"] == 700_000
    assert ctx["pending_count"] == 1
    assert ctx["flagged_count"] == 2
    html = r.content.decode()
    assert reverse("fees:manual_pending") in html
    assert reverse("fees:reconcile") in html
    assert reverse("fees:debtors") in html
    assert "₦7,000.00" in html


def test_home_links_to_dashboard(w, client_a):
    login_verified(client_a, w.bursar)
    html = client_a.get(reverse("home")).content.decode()
    assert f'href="{DASHBOARD}"' in html
