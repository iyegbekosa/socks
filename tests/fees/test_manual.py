"""Manual payments with two-person approval (UC-13)."""

import io
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import cloudinary
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context
from PIL import Image

from apps.audit.models import AuditEvent
from apps.fees import hooks, invoicing, ledger, manual
from apps.fees.ledger import PlannedLine
from apps.fees.models import FeeSettings, Invoice, ManualPayment, Payment, Receipt
from apps.tenancy.limits import PlanLimitExceeded
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_item, make_staff, make_structure
from tests.fees.test_ledger import _invoice, _world
from tests.results.test_cards import PNG, Uploads
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

LAGOS = ZoneInfo("Africa/Lagos")
PAID_ON = date(2026, 10, 1)
REASON = "Teller stamp is not legible, ask the parent for a clearer copy"


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture(autouse=True)
def _no_receipt_pdf():
    with mock.patch.object(hooks, "on_payment_confirmed") as hook:
        yield hook


@pytest.fixture
def uploads():
    u = Uploads()
    with (
        mock.patch("cloudinary.uploader.upload", side_effect=u.upload),
        mock.patch("cloudinary.uploader.destroy", side_effect=u.destroy),
    ):
        yield u


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "JPEG")
    return buf.getvalue()


def _png_upload(name="teller.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


def _mworld():
    w = _world()
    w.admin = make_staff("school_admin")
    w.inv = _invoice(w.student, w.term1, 10_000_000)
    return w


def _record(w, amount, *, plan=None, method="cash", **kw):
    kw.setdefault("paid_on", PAID_ON)
    kw.setdefault("reference_no", "" if method == "cash" else "TLR-0001")
    kw.setdefault("by", w.bursar)
    return manual.record(
        student=w.student,
        method=method,
        amount_kobo=amount,
        plan=plan if plan is not None else [PlannedLine(w.inv.pk, amount)],
        **kw,
    )


# --- service ------------------------------------------------------------------------------


def test_below_threshold_confirms_at_once(school_a, django_capture_on_commit_callbacks):
    with schema_context("school_a"):
        w = _mworld()
        with django_capture_on_commit_callbacks(execute=True):
            mp = _record(w, 2_000_000)
        mp.refresh_from_db()
        assert mp.status == ManualPayment.APPROVED
        assert mp.decided_by == w.bursar and mp.decided_at is not None
        payment = Payment.objects.get(manual=mp)
        assert payment.recorded_by == w.bursar and payment.approved_by == w.bursar
        assert payment.method == "cash"
        assert payment.amount_kobo == 2_000_000
        assert payment.paid_at == datetime(2026, 10, 1, 12, 0, tzinfo=LAGOS)
        assert Receipt.objects.filter(payment=payment).exists()
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 2_000_000 and w.inv.status == Invoice.PART_PAID
        ev = AuditEvent.objects.get(action="fees.manual_recorded")
        assert ev.after == {
            "manual_id": mp.pk,
            "student_id": w.student.pk,
            "method": "cash",
            "amount_kobo": 2_000_000,
            "status": "approved",
            "invoices": [{"invoice_id": w.inv.pk, "amount_kobo": 2_000_000}],
        }


def test_threshold_is_inclusive_and_configurable(school_a):
    with schema_context("school_a"):
        w = _mworld()
        assert _record(w, 5_000_000).status == ManualPayment.PENDING
        conf = FeeSettings.load()
        conf.manual_approval_threshold_kobo = 1_000_000
        conf.save()
        assert _record(w, 999_999).status == ManualPayment.APPROVED
        assert _record(w, 1_000_000).status == ManualPayment.PENDING


def test_teller_above_threshold_waits_then_admin_approves(
    school_a, uploads, django_capture_on_commit_callbacks, _no_receipt_pdf
):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000, method="teller", photo=_png_upload())
        assert mp.status == ManualPayment.PENDING
        assert mp.photo_name.startswith("fees/tellers/") and mp.photo_name.endswith(".png")
        assert f"dev/school_a/{mp.photo_name[:-4]}" in uploads.files
        assert not Payment.objects.exists()
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 0 and w.inv.status == Invoice.UNPAID

        with pytest.raises(PermissionDenied):
            manual.approve(mp, by=w.bursar)
        assert not Payment.objects.exists()

        with django_capture_on_commit_callbacks(execute=True):
            payment = manual.approve(mp, by=w.admin)
        _no_receipt_pdf.assert_called_once_with(payment.pk)
        mp.refresh_from_db()
        assert mp.status == ManualPayment.APPROVED and mp.decided_by == w.admin
        assert payment.manual == mp and payment.method == "teller"
        assert payment.recorded_by == w.bursar and payment.approved_by == w.admin
        assert Receipt.objects.filter(payment=payment).exists()
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 6_000_000
        ev = AuditEvent.objects.get(action="fees.manual_approved")
        assert ev.after == {"manual_id": mp.pk, "payment_id": payment.pk, "amount_kobo": 6_000_000}

        with pytest.raises(manual.ManualStateError):
            manual.approve(mp, by=w.admin)
        with pytest.raises(manual.ManualStateError):
            manual.reject(mp, reason=REASON, by=w.admin)
        assert Payment.objects.count() == 1


def test_approve_locks_the_manual_row(school_a):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        with CaptureQueriesContext(connection) as ctx:
            manual.approve(mp, by=w.admin)
        locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        assert '"fees_manualpayment"' in locks[0]


def test_reject_keeps_reason_on_row_only(school_a):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        with pytest.raises(PermissionDenied):
            manual.reject(mp, reason=REASON, by=w.bursar)
        with pytest.raises(ValidationError):
            manual.reject(mp, reason="   ", by=w.admin)
        manual.reject(mp, reason=REASON, by=w.admin)
        mp.refresh_from_db()
        assert mp.status == ManualPayment.REJECTED and mp.reject_reason == REASON
        assert mp.decided_by == w.admin
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 0 and not Payment.objects.exists()
        ev = AuditEvent.objects.get(action="fees.manual_rejected")
        assert ev.after == {"manual_id": mp.pk, "amount_kobo": 6_000_000}
        assert "legible" not in str(ev.before) + str(ev.after)
        with pytest.raises(manual.ManualStateError):
            manual.approve(mp, by=w.admin)


def test_approval_recaps_through_ledger(school_a):
    """Balance changed after recording: the ledger caps and flags, nothing is lost."""
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        _record(w, 4_900_000)  # below threshold, confirms now: balance left 5,100,000
        payment = manual.approve(mp, by=w.admin)
        assert payment.unallocated_kobo == 900_000 and payment.flagged


def _err_keys(exc) -> set[str]:
    return set(exc.value.message_dict)


@pytest.mark.parametrize(
    "case, key",
    [
        ("teller_without_reference", "reference_no"),
        ("pos_without_reference", "reference_no"),
        ("future_paid_on", "paid_on"),
        ("plan_sum_mismatch", "invoices"),
        ("other_students_invoice", "invoices"),
        ("line_above_balance", "invoices"),
        ("carried_invoice", "invoices"),
        ("zero_line", "invoices"),
        ("empty_plan", "invoices"),
        ("online_method", "method"),
        ("amount_over_cap", "amount"),
    ],
)
def test_validation_errors(school_a, case, key):
    with schema_context("school_a"):
        w = _mworld()
        kw: dict = {}
        amount = 1_000_000
        if case == "teller_without_reference":
            kw = {"method": "teller", "reference_no": "  "}
        elif case == "pos_without_reference":
            kw = {"method": "pos", "reference_no": ""}
        elif case == "future_paid_on":
            kw = {"paid_on": timezone.localdate() + timedelta(days=1)}
        elif case == "plan_sum_mismatch":
            kw = {"plan": [PlannedLine(w.inv.pk, 900_000)]}
        elif case == "other_students_invoice":
            other = _invoice(StudentProfileFactory(), w.term1, 5_000_000)
            kw = {"plan": [PlannedLine(other.pk, amount)]}
        elif case == "line_above_balance":
            small = _invoice(w.student, w.term2, 500_000)
            kw = {"plan": [PlannedLine(small.pk, amount)]}
        elif case == "carried_invoice":
            carried = _invoice(w.student, w.term2, 5_000_000, status=Invoice.CARRIED)
            kw = {"plan": [PlannedLine(carried.pk, amount)]}
        elif case == "zero_line":
            kw = {"plan": [PlannedLine(w.inv.pk, amount), PlannedLine(w.inv.pk, 0)]}
        elif case == "empty_plan":
            kw = {"plan": []}
        elif case == "online_method":
            kw = {"method": "online", "reference_no": "X"}
        elif case == "amount_over_cap":
            amount = 10_000_000_001
        with pytest.raises(ValidationError) as exc:
            _record(w, amount, **kw)
        assert key in _err_keys(exc)
        assert not ManualPayment.objects.exists() and not Payment.objects.exists()


def test_today_is_allowed_and_two_invoices_in_order(school_a):
    with schema_context("school_a"):
        w = _mworld()
        inv2 = _invoice(w.student, w.term2, 3_000_000)
        mp = _record(
            w,
            4_000_000,
            plan=[PlannedLine(inv2.pk, 3_000_000), PlannedLine(w.inv.pk, 1_000_000)],
            paid_on=timezone.localdate(),
        )
        assert mp.plan == [
            {"invoice_id": inv2.pk, "amount_kobo": 3_000_000},
            {"invoice_id": w.inv.pk, "amount_kobo": 1_000_000},
        ]
        assert mp.status == ManualPayment.APPROVED


def test_ledger_returns_existing_payment_for_manual(school_a):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        kw = {
            "student": w.student,
            "amount_kobo": 6_000_000,
            "method": "cash",
            "plan": [PlannedLine(w.inv.pk, 6_000_000)],
            "paid_at": timezone.now(),
            "manual": mp,
            "recorded_by": w.bursar,
            "approved_by": w.admin,
        }
        first = ledger.confirm_payment(**kw)
        second = ledger.confirm_payment(**kw)
        assert first.pk == second.pk
        assert Payment.objects.count() == 1 and Receipt.objects.count() == 1
        w.inv.refresh_from_db()
        assert w.inv.paid_kobo == 6_000_000


# --- photos -------------------------------------------------------------------------------


def test_photo_rules(school_a, uploads):
    with schema_context("school_a"):
        w = _mworld()
        ok_jpeg = SimpleUploadedFile("t.jpg", _jpeg(), content_type="image/jpeg")
        assert _record(w, 6_000_000, method="teller", photo=ok_jpeg).photo_name.endswith(".jpg")
        pdf = SimpleUploadedFile("t.pdf", b"%PDF-1.4\n%%EOF\n", content_type="application/pdf")
        assert _record(w, 6_000_000, method="teller", photo=pdf).photo_name.endswith(".pdf")
        bad = [
            SimpleUploadedFile("t.gif", b"GIF89a....", content_type="image/gif"),
            SimpleUploadedFile("t.png", b"not really a png", content_type="image/png"),
            SimpleUploadedFile("t.pdf", b"<html>nope</html>", content_type="application/pdf"),
            SimpleUploadedFile("t.jpg", PNG, content_type="image/jpeg"),
            SimpleUploadedFile("t.pdf", b"%PDF-" + b"0" * (5 * 1024 * 1024)),
        ]
        for upload in bad:
            with pytest.raises(ValidationError) as exc:
                _record(w, 6_000_000, method="teller", photo=upload)
            assert "photo" in _err_keys(exc)
        assert ManualPayment.objects.count() == 2


def test_photo_over_plan_limit(school_a, uploads):
    with schema_context("school_a"):
        w = _mworld()
        with (
            mock.patch(
                "apps.tenancy.limits.PlanLimits.check_can_store",
                side_effect=PlanLimitExceeded("Storage limit for your plan has been reached."),
            ),
            pytest.raises(PlanLimitExceeded),
        ):
            _record(w, 6_000_000, method="teller", photo=_png_upload())
        assert not ManualPayment.objects.exists() and not uploads.files


# --- views --------------------------------------------------------------------------------


def _form(w, *, amount="20000", method="cash", ref="", paid_on="2026-10-01", lines=None, **extra):
    data = {"method": method, "amount": amount, "paid_on": paid_on, "reference_no": ref}
    for inv, naira in lines or [(w.inv, amount)]:
        data[f"invoice_{inv.pk}"] = naira
    data.update(extra)
    return data


def test_record_page_preselects_invoice(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        _invoice(w.student, w.term2, 3_000_000)
        login_verified(client_a, w.bursar)
        url = reverse("fees:manual_new", args=[w.student.pk])
        r = client_a.get(f"{url}?invoice={w.inv.pk}")
        assert r.status_code == 200
        body = r.content.decode()
        assert f'name="invoice_{w.inv.pk}"' in body and 'value="100000.00"' in body
        assert client_a.get(f"{url}?invoice=abc").status_code == 404
        other = _invoice(StudentProfileFactory(), w.term1, 5_000_000)
        assert client_a.get(f"{url}?invoice={other.pk}").status_code == 404
        assert client_a.get(reverse("fees:manual_new", args=["x1"])).status_code == 404


def test_record_page_roles(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        url = reverse("fees:manual_new", args=[w.student.pk])
        client_a.force_login(make_staff("proprietor"))
        assert client_a.get(url).status_code == 403
        client_a.force_login(make_staff("teacher"))
        assert client_a.get(url).status_code == 403
        login_verified(client_a, w.admin)
        assert client_a.get(url).status_code == 200


def test_record_via_form_below_threshold(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        login_verified(client_a, w.bursar)
        r = client_a.post(reverse("fees:manual_new", args=[w.student.pk]), _form(w))
        assert r.status_code == 302, r.content.decode()
        assert r["Location"] == reverse("fees:invoice", args=[w.inv.pk])
        mp = ManualPayment.objects.get()
        assert mp.status == ManualPayment.APPROVED and mp.amount_kobo == 2_000_000
        assert Payment.objects.get().manual == mp


def test_record_via_form_errors(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        login_verified(client_a, w.bursar)
        url = reverse("fees:manual_new", args=[w.student.pk])
        r = client_a.post(url, _form(w, method="teller", amount="60000"))
        assert r.status_code == 200
        assert "Enter the teller or reference number." in r.content.decode()
        r = client_a.post(url, _form(w, amount="60000", lines=[(w.inv, "50000")]))
        assert "The invoice amounts must add up to the amount paid." in r.content.decode()
        r = client_a.post(url, _form(w, paid_on="2999-01-01"))
        assert "The date paid cannot be in the future." in r.content.decode()
        assert not ManualPayment.objects.exists()


def test_form_upload_errors(school_a, client_a, uploads):
    with schema_context("school_a"):
        w = _mworld()
        login_verified(client_a, w.bursar)
        url = reverse("fees:manual_new", args=[w.student.pk])
        big = SimpleUploadedFile("t.png", b"\x89PNG" + b"0" * (5 * 1024 * 1024))
        r = client_a.post(url, _form(w, method="teller", ref="T1", photo=big))
        assert "The photo must be 5 MB or smaller." in r.content.decode()
        gif = SimpleUploadedFile("t.gif", b"GIF89a")
        r = client_a.post(url, _form(w, method="teller", ref="T1", photo=gif))
        assert "Upload a JPEG, PNG or PDF file." in r.content.decode()
        with mock.patch(
            "apps.tenancy.limits.PlanLimits.check_can_store",
            side_effect=PlanLimitExceeded("Storage limit for your plan has been reached."),
        ):
            r = client_a.post(url, _form(w, method="teller", ref="T1", photo=_png_upload()))
        assert r.status_code == 200
        assert "Storage limit for your plan has been reached." in r.content.decode()
        assert not ManualPayment.objects.exists()


def test_pending_list_and_decisions(school_a, client_a, uploads):
    with schema_context("school_a"):
        w = _mworld()
        login_verified(client_a, w.bursar)
        r = client_a.post(
            reverse("fees:manual_new", args=[w.student.pk]),
            _form(w, amount="60000", method="teller", ref="TLR-77", photo=_png_upload()),
        )
        assert r.status_code == 302 and r["Location"] == reverse("fees:manual_pending")
        mp = ManualPayment.objects.get()
        assert mp.status == ManualPayment.PENDING
        decide = reverse("fees:manual_decide", args=[mp.pk])

        body = client_a.get(reverse("fees:manual_pending")).content.decode()
        assert "TLR-77" in body and "₦60,000.00" in body
        assert "You recorded this payment" in body and decide not in body
        assert client_a.post(decide, {"action": "approve"}).status_code == 403

        client_a.force_login(make_staff("proprietor"))
        r = client_a.get(reverse("fees:manual_pending"))
        assert r.status_code == 200 and "TLR-77" in r.content.decode()
        assert decide not in r.content.decode()
        assert client_a.post(decide, {"action": "approve"}).status_code == 403

        login_verified(client_a, w.admin)
        assert decide in client_a.get(reverse("fees:manual_pending")).content.decode()
        assert client_a.get(decide).status_code == 405
        assert client_a.post(decide, {"action": "nope"}).status_code == 400
        r = client_a.post(decide, {"action": "approve"})
        assert r.status_code == 302
        assert Payment.objects.get().manual_id == mp.pk
        r = client_a.post(decide, {"action": "approve"})
        assert r.status_code == 409
        assert Payment.objects.count() == 1


def test_reject_via_view_needs_reason(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        login_verified(client_a, w.admin)
        decide = reverse("fees:manual_decide", args=[mp.pk])
        r = client_a.post(decide, {"action": "reject", "reason": ""})
        assert r.status_code == 400 and "Give a reason" in r.content.decode()
        r = client_a.post(decide, {"action": "reject", "reason": REASON})
        assert r.status_code == 302
        mp.refresh_from_db()
        assert mp.status == ManualPayment.REJECTED and mp.reject_reason == REASON
        assert client_a.post(reverse("fees:manual_decide", args=["x"])).status_code == 404


def test_teller_photo_route(school_a, client_a, uploads):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000, method="teller", photo=_png_upload())
        no_photo = _record(w, 6_000_000)
        url = reverse("fees:teller_photo", args=[mp.pk])

        login_verified(client_a, w.bursar)
        r = client_a.get(url)
        assert r.status_code == 302 and "no-store" in r["Cache-Control"]
        assert r["Location"].split("?")[0] == default_storage.url(mp.photo_name).split("?")[0]
        assert client_a.get(reverse("fees:teller_photo", args=[no_photo.pk])).status_code == 404

        client_a.force_login(make_staff("proprietor"))
        assert client_a.get(url).status_code == 302
        client_a.force_login(make_staff("teacher"))
        assert client_a.get(url).status_code == 403
        client_a.force_login(w.student.user)
        assert client_a.get(url).status_code == 403


def test_links(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        login_verified(client_a, w.bursar)
        body = client_a.get(reverse("fees:invoice", args=[w.inv.pk])).content.decode()
        expected = f"{reverse('fees:manual_new', args=[w.student.pk])}?invoice={w.inv.pk}"
        assert expected in body and "Record payment" in body
        body = client_a.get(reverse("fees:structures")).content.decode()
        assert reverse("fees:manual_pending") in body and "Pending approvals" in body
        client_a.force_login(make_staff("proprietor"))
        body = client_a.get(reverse("fees:invoice", args=[w.inv.pk])).content.decode()
        assert "Record payment" not in body


# --- fix round 1 --------------------------------------------------------------------------


def _settings_post(threshold="1000000", **extra):
    data = {
        "allow_part_payment": "on",
        "min_part_payment": "0",
        "manual_approval_threshold": threshold,
        "bank_transfer_note": "",
    }
    data.update(extra)
    return data


def test_bursar_cannot_change_threshold(school_a, client_a):
    with schema_context("school_a"):
        bursar = make_staff("bursar")
        FeeSettings.load()
    login_verified(client_a, bursar)
    r = client_a.get(reverse("fees:settings"))
    assert r.status_code == 200
    body = r.content.decode()
    assert 'name="manual_approval_threshold"' in body and "disabled" in body
    # a forged POST changes the other fields but never the threshold
    r = client_a.post(reverse("fees:settings"), _settings_post(min_part_payment="500"))
    assert r.status_code == 302
    with schema_context("school_a"):
        conf = FeeSettings.load()
        assert conf.manual_approval_threshold_kobo == 5_000_000
        assert conf.min_part_payment_kobo == 50_000
        ev = AuditEvent.objects.get(action="fees.settings_saved")
        assert "manual_approval_threshold_kobo" not in ev.after
        assert ev.before == {"min_part_payment_kobo": 0}
        assert ev.after == {"min_part_payment_kobo": 50_000}
        with pytest.raises(PermissionDenied):
            from apps.fees import services

            services.save_settings({"manual_approval_threshold_kobo": 1}, by=bursar)
        assert FeeSettings.load().manual_approval_threshold_kobo == 5_000_000


def test_admin_changes_threshold_with_before_after_audit(school_a, client_a):
    with schema_context("school_a"):
        admin = make_staff("school_admin")
        FeeSettings.load()
    login_verified(client_a, admin)
    body = client_a.get(reverse("fees:settings")).content.decode()
    assert 'name="manual_approval_threshold"' in body
    r = client_a.post(reverse("fees:settings"), _settings_post(threshold="10000"))
    assert r.status_code == 302
    with schema_context("school_a"):
        assert FeeSettings.load().manual_approval_threshold_kobo == 1_000_000
        ev = AuditEvent.objects.get(action="fees.settings_saved")
        assert ev.before == {"manual_approval_threshold_kobo": 5_000_000}
        assert ev.after == {"manual_approval_threshold_kobo": 1_000_000}


def test_manual_payment_keeps_threshold_in_force(school_a):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        assert mp.threshold_kobo == 5_000_000
        conf = FeeSettings.load()
        conf.manual_approval_threshold_kobo = 1_000_000
        conf.save()
        assert _record(w, 500_000).threshold_kobo == 1_000_000
        mp.refresh_from_db()
        assert mp.threshold_kobo == 5_000_000


def test_dual_role_user_cannot_approve_own_record(school_a):
    from apps.accounts.roles import set_roles

    with schema_context("school_a"):
        w = _mworld()
        set_roles(w.bursar, ["bursar", "school_admin"], by=None)
        mp = _record(w, 6_000_000)
        with pytest.raises(PermissionDenied):
            manual.approve(mp, by=w.bursar)
        with pytest.raises(PermissionDenied):
            manual.reject(mp, reason=REASON, by=w.bursar)
        assert not Payment.objects.exists()


def test_decide_checks_permission_and_state_before_reason(school_a, client_a):
    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000)
        decided = _record(w, 6_000_000)
        manual.reject(decided, reason=REASON, by=w.admin)
    login_verified(client_a, w.bursar)
    r = client_a.post(reverse("fees:manual_decide", args=[mp.pk]), {"action": "reject"})
    assert r.status_code == 403
    login_verified(client_a, w.admin)
    r = client_a.post(reverse("fees:manual_decide", args=[decided.pk]), {"action": "reject"})
    assert r.status_code == 409


def test_paid_at_is_noon_lagos_whatever_the_active_timezone(school_a):
    with timezone.override(ZoneInfo("UTC")):
        assert manual.paid_at_for(PAID_ON) == datetime(2026, 10, 1, 11, 0, tzinfo=ZoneInfo("UTC"))
        assert manual.paid_at_for(PAID_ON).astimezone(LAGOS).hour == 12


@pytest.mark.pdf
def test_receipt_shows_teller_number(school_a, uploads, django_capture_on_commit_callbacks):
    from apps.fees import receipts
    from tests.results.test_cards import pdf_text

    with schema_context("school_a"):
        w = _mworld()
        mp = _record(w, 6_000_000, method="teller", reference_no="TLR-55501")
        payment = manual.approve(mp, by=w.admin)
        assert receipts.generate_receipt(payment.pk) is True
        receipt = Receipt.objects.get(payment=payment)
        text = pdf_text(uploads.files[f"dev/school_a/receipts/{receipt.year}/{receipt.pk}.pdf"])
        assert "TLR-55501" in text


def test_pending_approval_then_next_term_generated_pays_the_new_invoice(school_a):
    """I1 / Ruling Q: a manual payment waits for approval while 2nd-term invoices are
    generated; on approval the money follows the carried debt to the new invoice."""
    with schema_context("school_a"):
        w = _mworld()  # 1st-term invoice of 100,000
        mp = _record(w, 6_000_000, method="teller")
        assert mp.status == ManualPayment.PENDING
        make_structure(w.level, w.term2, [(make_item("Tuition"), 2_000_000, False)])
        invoicing.generate_invoices(w.term2, by=w.bursar)
        term2_inv = Invoice.objects.get(student=w.student, term=w.term2)
        assert term2_inv.total_kobo == 12_000_000
        payment = manual.approve(mp, by=w.admin)
        assert not payment.flagged and payment.unallocated_kobo == 0
        assert list(payment.allocations.values_list("invoice_id", "amount_kobo")) == [
            (term2_inv.pk, 6_000_000)
        ]
        term2_inv.refresh_from_db()
        assert term2_inv.paid_kobo == 6_000_000
        w.inv.refresh_from_db()
        assert w.inv.status == Invoice.CARRIED and w.inv.paid_kobo == 0
