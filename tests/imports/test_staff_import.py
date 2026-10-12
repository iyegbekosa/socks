import csv
import io
from pathlib import Path

import openpyxl
import pytest
from django.core import mail
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.storage import unique_upload_name
from apps.imports.models import ImportJob
from apps.imports.staff import TEMPLATE_HEADERS
from apps.imports.tasks import run_import
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.imports.conftest import csv_file

pytestmark = pytest.mark.django_db

H = ["full_name", "email", "phone", "roles"]
BASE = "http://school-a.localhost"


def srow(**over) -> dict:
    row = {"full_name": "Ada Obi", "email": "ada@school.ng", "phone": "", "roles": ""}
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _clean(world):
    with schema_context("school_a"):
        User.objects.exclude(pk=world.admin.pk).filter(kind="staff").delete()
    yield
    with schema_context("school_a"):
        User.objects.exclude(pk=world.admin.pk).filter(kind="staff").delete()


def run_job(world, rows, *, verified=True, capture=None) -> ImportJob:
    content = csv_file(rows, H)
    with schema_context("school_a"):
        name = default_storage.save(unique_upload_name("imports", "s.csv"), ContentFile(content))
        job = ImportJob.objects.create(
            kind="staff",
            file_name=name,
            original_name="s.csv",
            created_by=world.admin,
            started_verified=verified,
            base_url=BASE,
        )
        if capture:
            with capture(execute=True):
                run_import.delay(job.pk)
        else:
            run_import.delay(job.pk)
        job.refresh_from_db()
        return job


def test_three_rows_three_invites(world, cloud, django_capture_on_commit_callbacks):
    rows = [srow(email=f"s{i}@school.ng", full_name=f"Staff {i}") for i in range(3)]
    job = run_job(world, rows, capture=django_capture_on_commit_callbacks)
    assert job.status == "completed"
    assert (job.total_rows, job.ok_rows, job.error_rows, job.invited_count) == (3, 3, 0, 3)
    assert len(mail.outbox) == 3
    assert BASE + "/accounts/invite/" in mail.outbox[0].body
    with schema_context("school_a"):
        u = User.objects.get(email="s0@school.ng")
        assert u.roles == {"teacher"}
        assert AuditEvent.objects.filter(action="user.invited").count() >= 3


def test_unknown_role_row_error_names_role_only(world, cloud):
    job = run_job(world, [srow(roles="teacher, wizard"), srow(email="b@school.ng")])
    assert (job.ok_rows, job.error_rows) == (1, 1)
    err = job.errors[0]
    assert err["row"] == 2 and "wizard" in err["message"]
    assert "ada@school.ng" not in str(job.errors)


def test_roles_are_trimmed_lowercased_and_comma_separated(world, cloud):
    job = run_job(world, [srow(roles=" Teacher , PRINCIPAL ")])
    assert job.ok_rows == 1
    with schema_context("school_a"):
        assert User.objects.get(email="ada@school.ng").roles == {"teacher", "principal"}


def test_mfa_role_needs_verified_upload(world, cloud):
    rows = [srow(email="bur@school.ng", roles="bursar"), srow(email="t@school.ng")]
    job = run_job(world, rows, verified=False)
    assert (job.ok_rows, job.error_rows) == (1, 1)
    assert job.errors[0]["row"] == 2
    assert job.errors[0]["message"] == "This role needs a two-factor-verified admin to import"
    with schema_context("school_a"):
        assert not User.objects.filter(email="bur@school.ng").exists()
    job = run_job(world, [srow(email="bur@school.ng", roles="bursar")], verified=True)
    assert job.ok_rows == 1


def test_duplicate_phone_in_file_and_against_existing(world, cloud):
    with schema_context("school_a"):
        StaffFactory(email="", phone="+2348055555555")
    rows = [
        srow(email="", phone="08031234567"),  # row 2 ok
        srow(email="", phone="8031234567"),  # row 3 dup in file (bare number)
        srow(email="", phone="08055555555"),  # row 4 existing staff
        srow(email="x@school.ng"),  # row 5 ok
    ]
    job = run_job(world, rows)
    assert (job.ok_rows, job.error_rows) == (2, 2)
    assert [e["row"] for e in job.errors] == [3, 4]
    assert {e["message"] for e in job.errors} == {"Email or phone already used"}
    assert "8031234567" not in str(job.errors) and "8055555555" not in str(job.errors)


def test_duplicate_email_against_existing_and_in_file(world, cloud):
    with schema_context("school_a"):
        StaffFactory(email="dup@school.ng")
    rows = [srow(email="DUP@school.ng"), srow(email="n@school.ng"), srow(email="n@school.ng")]
    job = run_job(world, rows)
    assert [e["row"] for e in job.errors] == [2, 4]
    assert "dup@school.ng" not in str(job.errors).lower()


def test_validation_errors_do_not_echo_values(world, cloud):
    rows = [
        srow(full_name="", email="a@school.ng"),
        srow(email="not-an-email"),
        srow(email="", phone="0803"),
        srow(email="", phone=""),
    ]
    job = run_job(world, rows)
    assert job.error_rows == 4 and job.ok_rows == 0
    assert [e["row"] for e in job.errors] == [2, 3, 4, 5]
    assert "not-an-email" not in str(job.errors)


def test_missing_full_name_column_fails_job(world, cloud):
    with schema_context("school_a"):
        name = default_storage.save(
            unique_upload_name("imports", "s.csv"), ContentFile(b"email\r\na@b.ng\r\n")
        )
        job = ImportJob.objects.create(kind="staff", file_name=name, created_by=world.admin)
        run_import.delay(job.pk)
        job.refresh_from_db()
    assert job.status == "failed" and "full_name" in job.message


def test_upload_stores_verified_and_base_url_and_shows_invites(
    client_a, world, cloud, django_capture_on_commit_callbacks
):
    login_verified(client_a, world.admin)
    data = csv_file([srow(), srow(email="b@school.ng")], H)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(
            reverse("imports:new"),
            {"kind": "staff", "file": SimpleUploadedFile("s.csv", data)},
            HTTP_HOST="school-a.localhost",
        )
    assert r.status_code == 302
    with schema_context("school_a"):
        job = ImportJob.objects.get(kind="staff")
    assert job.started_verified is True
    assert job.base_url == "http://school-a.localhost"
    assert job.status == "completed" and job.invited_count == 2
    assert len(mail.outbox) == 2
    body = client_a.get(reverse("imports:detail", args=[job.pk])).content.decode()
    assert "Invites sent: 2" in body
    assert "Print passwords" not in body
    assert client_a.post(reverse("imports:passwords", args=[job.pk])).status_code == 404


def test_staff_template_files_have_exact_headers():
    base = Path(__file__).resolve().parents[2] / "apps/imports/static/imports"
    text = (base / "staff_template.csv").read_text(encoding="utf-8")
    assert list(csv.reader(io.StringIO(text))) == [list(TEMPLATE_HEADERS)] == [H]
    wb = openpyxl.load_workbook(base / "staff_template.xlsx", read_only=True)
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    assert rows == [tuple(H)]


def test_mail_failure_after_commit_still_counts_row_ok(
    world, cloud, django_capture_on_commit_callbacks, caplog
):
    from unittest import mock

    with (
        mock.patch("apps.accounts.services.send_mail", side_effect=OSError("smtp down")),
        caplog.at_level("WARNING", logger="apps.accounts.services"),
    ):
        job = run_job(world, [srow()], capture=django_capture_on_commit_callbacks)
    assert (job.ok_rows, job.error_rows) == (1, 0)
    with schema_context("school_a"):
        assert User.objects.filter(email="ada@school.ng").exists()
    assert "Invite send failed: OSError" in caplog.text
    assert "ada@school.ng" not in caplog.text


def test_roles_cell_with_unsafe_text_gets_generic_message(world, cloud):
    job = run_job(world, [srow(roles="leak@school.ng"), srow(email="b@school.ng", roles="Wizard")])
    msgs = [e["message"] for e in job.errors]
    assert msgs == ["Unknown role in the roles column", "Unknown role “wizard”."]
    assert "leak@school.ng" not in str(job.errors)


def test_unverified_school_admin_upload_cannot_import_bursar(client_a, world, cloud):
    client_a.force_login(world.admin)  # no OTP verification
    data = csv_file([srow(email="bur@school.ng", roles="bursar")], H)
    r = client_a.post(
        reverse("imports:new"),
        {"kind": "staff", "file": SimpleUploadedFile("s.csv", data)},
        HTTP_HOST="school-a.localhost",
    )
    with schema_context("school_a"):
        job = ImportJob.objects.filter(kind="staff").first()
        assert not User.objects.filter(email="bur@school.ng").exists()
    if job is None:
        # MfaEnforcementMiddleware keeps an unverified admin off the page entirely.
        assert r.status_code in (302, 403)
    else:
        assert job.started_verified is False
        assert job.error_rows == 1
