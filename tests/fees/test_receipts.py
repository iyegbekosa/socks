"""Receipt PDFs, downloads and guardian receipt messages (FR-14)."""

import logging
from datetime import UTC, datetime
from unittest import mock

import cloudinary
import pytest
from django.core import mail
from django.core.files.storage import default_storage
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.core.logging import SECRET_ARG_TASKS
from apps.fees import hooks, receipts, tasks
from apps.fees.ledger import PlannedLine, confirm_payment
from apps.fees.models import Receipt
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff
from tests.fees.test_ledger import _invoice, _world
from tests.results.test_cards import PNG, Uploads, pdf_text  # noqa: F401
from tests.students.factories import StudentProfileFactory

pytestmark = [pytest.mark.django_db, pytest.mark.pdf]

PAID_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
BODY = "Payment of ₦30,000.00 received for Ada. Receipt GFS/RCT/{year}/000001."


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture
def uploads():
    u = Uploads()
    with (
        mock.patch("cloudinary.uploader.upload", side_effect=u.upload),
        mock.patch("cloudinary.uploader.destroy", side_effect=u.destroy),
    ):
        yield u


def _paid_world(*, email="Guardian@Example.com", email2="guardian@example.com"):
    """A student, two invoices (55,000 and 20,000), and a 30,000 naira payment (20,000 +
    10,000), leaving balances of 35,000 and 10,000."""
    w = _world()
    w.student.first_name = "Ada"
    w.student.guardian_email = email
    w.student.guardian2_email = email2
    w.student.save()
    w.inv1 = _invoice(w.student, w.term1, 5_500_000)
    w.inv2 = _invoice(w.student, w.term2, 2_000_000)
    with mock.patch.object(hooks, "on_payment_confirmed"):
        w.payment = confirm_payment(
            student=w.student,
            amount_kobo=3_000_000,
            method="cash",
            plan=[PlannedLine(w.inv1.pk, 2_000_000), PlannedLine(w.inv2.pk, 1_000_000)],
            paid_at=PAID_AT,
            recorded_by=w.bursar,
            approved_by=w.bursar,
        )
    w.receipt = Receipt.objects.get(payment=w.payment)
    return w


def _stored_pdf(uploads, receipt):
    return uploads.files[f"dev/school_a/receipts/{receipt.year}/{receipt.pk}.pdf"]


def test_pdf_contents(school_a, uploads):
    with schema_context("school_a"):
        w = _paid_world()
        assert receipts.generate_receipt(w.payment.pk) is True
        w.receipt.refresh_from_db()
        assert w.receipt.pdf_name.endswith(f"receipts/{w.receipt.year}/{w.receipt.pk}.pdf")
        text = pdf_text(_stored_pdf(uploads, w.receipt))
        assert w.receipt.number in text
        assert w.student.full_name in text
        assert w.student.admission_number in text
        assert "₦30,000.00" in text
        assert str(w.term1) in text and str(w.term2) in text
        assert "₦35,000.00" in text and "₦10,000.00" in text  # remaining balances 35k and 10k
        assert "Recorded by" in text and w.bursar.get_full_name() in text


def test_guardian_email_is_exact_deduped_and_has_no_balance(
    school_a, uploads, django_capture_on_commit_callbacks
):
    with schema_context("school_a"):
        w = _paid_world()
        mail.outbox.clear()
        # Email goes out on commit through ``comms.notify`` (P6).
        with django_capture_on_commit_callbacks(execute=True):
            tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
        year = w.receipt.year
        assert len(mail.outbox) == 1  # the two addresses differ only in case
        assert mail.outbox[0].body == BODY.format(year=year)
        assert "balance" not in mail.outbox[0].body.lower()


def test_no_sms_for_a_receipt_even_when_configured(
    school_a, uploads, sms_outbox, django_capture_on_commit_callbacks
):
    """Ruling AF: receipts are email and in-app only, even with an SMS backend configured."""
    with schema_context("school_a"):
        w = _paid_world()
        w.student.guardian_phone = "+2348012345678"
        w.student.save()
        mail.outbox.clear()
        with (
            mock.patch("apps.core.sms.send_sms") as sms,
            django_capture_on_commit_callbacks(execute=True),
        ):
            tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
        sms.assert_not_called()
        assert sms_outbox == []
        assert len(mail.outbox) == 1


def test_hook_enqueues_on_reports_queue_and_swallows_errors(school_a, caplog):
    with mock.patch.object(tasks.generate_receipt_task, "apply_async") as enq:
        hooks.on_payment_confirmed(7)
    enq.assert_called_once_with(args=[7], queue="reports")
    with (
        mock.patch.object(
            tasks.generate_receipt_task, "apply_async", side_effect=OSError("secret")
        ),
        caplog.at_level(logging.WARNING),
    ):
        hooks.on_payment_confirmed(7)  # must not raise
    assert "OSError" in caplog.text and "secret" not in caplog.text


def test_send_task_runs_only_after_a_stored_pdf(school_a, uploads):
    with schema_context("school_a"):
        w = _paid_world()
        mail.outbox.clear()
        with mock.patch("cloudinary.uploader.upload", side_effect=OSError("down")):
            assert receipts.generate_receipt(w.payment.pk) is False
        assert mail.outbox == []


def test_task_names_are_secret_arg_tasks():
    assert tasks.generate_receipt_task.name in SECRET_ARG_TASKS
    assert tasks.send_receipt_task.name in SECRET_ARG_TASKS
    assert tasks.generate_receipt_task.queue == "reports"
    assert tasks.send_receipt_task.queue == "notifications"


def test_storage_failure_leaves_pdf_blank_logs_type_and_regenerate_fixes(
    school_a, uploads, client_a, caplog, django_capture_on_commit_callbacks
):
    with schema_context("school_a"):
        w = _paid_world()
        with (
            mock.patch("cloudinary.uploader.upload", side_effect=OSError("boom-secret")),
            caplog.at_level(logging.WARNING),
        ):
            assert receipts.generate_receipt(w.payment.pk) is False
        assert "boom-secret" not in caplog.text
        w.receipt.refresh_from_db()
        assert w.receipt.pdf_name == ""
        login_verified(client_a, w.bursar)
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            r = client_a.post(reverse("fees:receipt_regenerate", args=[w.receipt.pk]))
        assert r.status_code == 302
        w.receipt.refresh_from_db()
        assert w.receipt.pdf_name != ""
        assert len(mail.outbox) == 1
        # regenerating an existing PDF does not message guardians again
        with django_capture_on_commit_callbacks(execute=True):
            client_a.post(reverse("fees:receipt_regenerate", args=[w.receipt.pk]))
        assert len(mail.outbox) == 1


def test_regenerate_requires_manage_post(school_a, uploads, client_a):
    with schema_context("school_a"):
        w = _paid_world()
        url = reverse("fees:receipt_regenerate", args=[w.receipt.pk])
        proprietor = make_staff("proprietor")
        login_verified(client_a, w.bursar)
        assert client_a.get(url).status_code == 405
        client_a.logout()
        client_a.force_login(proprietor)
        assert client_a.post(url).status_code == 403


def test_download_rules(school_a, uploads, client_a):
    with schema_context("school_a"):
        w = _paid_world()
        url = reverse("fees:receipt_download", args=[w.receipt.pk])
        other = StudentProfileFactory()
        teacher = make_staff("teacher")
        proprietor = make_staff("proprietor")

        # no PDF yet: the preparing page (200) for the paying student
        client_a.force_login(w.student.user)
        r = client_a.get(url)
        assert r.status_code == 200 and "Your receipt is being prepared." in r.text

        receipts.generate_receipt(w.payment.pk)
        r = client_a.get(url)
        assert r.status_code == 302
        assert "no-store" in r["Cache-Control"]
        expected = default_storage.url(Receipt.objects.get(pk=w.receipt.pk).pdf_name)
        # Signed URLs carry an expiry in the query string, so compare without it.
        assert r["Location"].split("?")[0] == expected.split("?")[0]

        client_a.force_login(other.user)
        assert client_a.get(url).status_code == 404

        client_a.force_login(teacher)
        assert client_a.get(url).status_code in (403, 404)

        client_a.force_login(proprietor)
        assert client_a.get(url).status_code == 302

        client_a.logout()
        login_verified(client_a, w.bursar)
        assert client_a.get(url).status_code == 302
        assert client_a.get(reverse("fees:receipt_download", args=[999999])).status_code == 404


def test_student_with_an_allocation_but_not_the_payer_may_download(school_a, uploads, client_a):
    with schema_context("school_a"):
        w = _paid_world()
        payer = StudentProfileFactory()
        with mock.patch.object(hooks, "on_payment_confirmed"):
            pay = confirm_payment(
                student=payer,
                amount_kobo=500_000,
                method="cash",
                plan=[PlannedLine(w.inv1.pk, 500_000)],
                paid_at=PAID_AT,
                recorded_by=w.bursar,
                approved_by=w.bursar,
            )
        url = reverse("fees:receipt_download", args=[pay.receipt.pk])
        client_a.force_login(w.student.user)  # owns the allocated invoice
        assert client_a.get(url).status_code == 200
        other = StudentProfileFactory()
        client_a.force_login(other.user)
        assert client_a.get(url).status_code == 404


def test_invoice_page_links_each_allocation_to_its_receipt(school_a, uploads, client_a):
    with schema_context("school_a"):
        w = _paid_world()
        login_verified(client_a, w.bursar)
        r = client_a.get(reverse("fees:invoice", args=[w.inv1.pk]))
        assert reverse("fees:receipt_download", args=[w.receipt.pk]) in r.text


def test_pdf_prints_balance_at_confirmation_not_later(school_a, uploads):
    """Ruling T: a later payment does not change the balance a receipt prints."""
    with schema_context("school_a"):
        w = _paid_world()
        al = w.payment.allocations.get(invoice=w.inv1)
        assert al.balance_after_kobo == 3_500_000
        assert w.payment.allocations.get(invoice=w.inv2).balance_after_kobo == 1_000_000
        with mock.patch.object(hooks, "on_payment_confirmed"):
            confirm_payment(
                student=w.student,
                amount_kobo=1_200_000,
                method="cash",
                plan=[PlannedLine(w.inv1.pk, 1_200_000)],
                paid_at=PAID_AT,
                recorded_by=w.bursar,
                approved_by=w.bursar,
            )
        assert receipts.generate_receipt(w.payment.pk) is True
        text = pdf_text(_stored_pdf(uploads, w.receipt))
        assert "₦35,000.00" in text  # as at the first payment
        assert "₦23,000.00" not in text  # the current balance


def test_allocation_without_snapshot_falls_back_to_current_balance(school_a):
    with schema_context("school_a"):
        w = _paid_world()
        w.payment.allocations.update(balance_after_kobo=None)
        lines = receipts.receipt_context(w.receipt, logo=None)["lines"]
        assert [ln["balance"] for ln in lines] == ["₦35,000.00", "₦10,000.00"]


def test_racing_runs_message_guardians_once(school_a, uploads):
    with schema_context("school_a"):
        w = _paid_world()
        assert receipts.store_receipt(w.payment.pk) == receipts.FIRST_STORED
        assert receipts.store_receipt(w.payment.pk) == receipts.STORED
        mail.outbox.clear()
        tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
        assert mail.outbox == []  # the PDF already existed: no second message


def test_message_enqueue_failure_is_logged_by_type_and_swallowed(school_a, uploads, caplog):
    with schema_context("school_a"):
        w = _paid_world()
        with (
            mock.patch.object(
                tasks.send_receipt_task, "apply_async", side_effect=OSError("broker-secret")
            ),
            caplog.at_level(logging.WARNING),
        ):
            tasks.generate_receipt_task.apply_async(args=[w.payment.pk], queue="reports")
        assert "OSError" in caplog.text and "broker-secret" not in caplog.text
        w.receipt.refresh_from_db()
        assert w.receipt.pdf_name != ""
