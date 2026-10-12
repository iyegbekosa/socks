import time
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.core.storage import unique_upload_name
from apps.imports.models import ImportJob
from apps.imports.tasks import run_import
from apps.students.models import Student
from apps.tenancy.models import Plan
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified
from tests.imports.conftest import HEADERS, csv_file, good_row, xlsx_file
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def run_job(world, content: bytes, ext: str = ".csv") -> ImportJob:
    """Store ``content`` privately, create a job and run it (eager Celery)."""
    with schema_context("school_a"):
        name = default_storage.save(unique_upload_name("imports", f"f{ext}"), ContentFile(content))
        job = ImportJob.objects.create(
            kind="students", file_name=name, original_name=f"f{ext}", created_by=world.admin
        )
        run_import.delay(job.pk)
        job.refresh_from_db()
        return job


def five_rows():
    return [
        good_row(first_name="Ada"),  # row 2
        good_row(first_name="Bola", guardian_phone="", guardian_email="b@mail.ng"),  # row 3
        good_row(first_name="Chi", **{"class": "JSS 9Z"}),  # row 4: unknown class
        good_row(first_name="Dayo", **{"class": "jss 2 gold"}),  # row 5
        good_row(first_name="Efe", guardian_phone="0803 999"),  # row 6: bad phone
    ]


def test_mixed_file_saves_good_rows_and_reports_bad_ones(world, cloud):
    job = run_job(world, csv_file(five_rows()))
    assert job.status == ImportJob.Status.COMPLETED
    assert (job.total_rows, job.ok_rows, job.error_rows) == (5, 3, 2)
    with schema_context("school_a"):
        names = set(Student.objects.values_list("first_name", flat=True))
        assert names == {"Ada", "Bola", "Dayo"}
        dayo = Student.objects.get(first_name="Dayo")
        assert dayo.enrolments.get().arm == world.b
        assert dayo.enrolments.get().session == world.session
        assert dayo.consent_method == "paper_form"
        assert dayo.consent_recorded_by == world.admin
        # No passwords at import: staff issue them per class afterwards.
        assert not dayo.user.has_usable_password()
        assert dayo.user.must_change_password
    assert [(e["row"], e["column"]) for e in job.errors] == [(4, "class"), (6, "guardian_phone")]
    assert "JSS 9Z" in job.errors[0]["message"]
    assert "999" not in job.errors[1]["message"]
    assert set(job.errors[0]) == {"row", "column", "message"}
    assert job.finished_at is not None


def test_xlsx_number_phone_and_date_cells(world, cloud):
    rows = [
        HEADERS,
        ["Ada", "", "Eze", "F", datetime(2014, 9, 5), "JSS 1A", "", "Ngozi", "", 8031234567, ""],
        ["Bola", "", "Eze", "M", "5/9/2014", "JSS 1A", "", "Ngozi", "", 8031234567.0, ""],
    ]
    job = run_job(world, xlsx_file(rows), ".xlsx")
    assert job.status == ImportJob.Status.COMPLETED, job.errors
    assert job.ok_rows == 2 and job.errors == []
    with schema_context("school_a"):
        for s in Student.objects.all():
            assert s.guardian_phone == "+2348031234567"
            assert s.date_of_birth == date(2014, 9, 5)
            assert s.guardian_relationship == "guardian"  # blank defaults to guardian


def test_row_errors_never_echo_sensitive_values(world, cloud):
    rows = [
        good_row(date_of_birth="31/31/2014", guardian_email="not-an-email"),
        good_row(guardian_phone="", guardian_email=""),
        good_row(sex="X", guardian_relationship="aunty"),
        good_row(date_of_birth="17/05/1990"),
    ]
    job = run_job(world, csv_file(rows))
    assert job.ok_rows == 0 and job.error_rows == 4
    cols = {(e["row"], e["column"]) for e in job.errors}
    assert {(2, "date_of_birth"), (2, "guardian_email"), (3, "guardian_phone")} <= cols
    assert {(4, "sex"), (4, "guardian_relationship"), (5, "date_of_birth")} <= cols
    text = repr(job.errors)
    for secret in ("31/31/2014", "not-an-email", "1990", "aunty"):
        assert secret not in text


def test_admission_numbers_duplicated_in_file_are_errors_after_the_first(world, cloud):
    rows = [
        good_row(first_name="Ada", admission_number="gfs/1"),
        good_row(first_name="Bola", admission_number="GFS/1"),
        good_row(first_name="Chi", admission_number="GFS/2"),
    ]
    job = run_job(world, csv_file(rows))
    assert job.ok_rows == 2
    assert [(e["row"], e["column"]) for e in job.errors] == [(3, "admission_number")]
    with schema_context("school_a"):
        assert Student.objects.get(first_name="Ada").user.admission_number == "GFS/1"


def test_admission_number_already_in_school(world, cloud):
    with schema_context("school_a"):
        StudentFactory(admission_number="GFS/7")
    job = run_job(world, csv_file([good_row(admission_number="GFS/7")]))
    assert job.ok_rows == 0
    assert job.errors[0]["column"] == "admission_number"


def test_unique_violation_race_becomes_row_error(world, cloud):
    from apps.imports import students as importer

    real = importer.admit_student
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            cause = Exception("duplicate key")
            cause.sqlstate = "23505"
            cause.diag = SimpleNamespace(constraint_name="accounts_user_admission_number_key")
            raise IntegrityError("duplicate key") from cause
        return real(**kwargs)

    with mock.patch.object(importer, "admit_student", side_effect=flaky):
        job = run_job(world, csv_file([good_row(first_name="Ada"), good_row(first_name="Bola")]))
    assert job.status == ImportJob.Status.COMPLETED
    assert job.errors == [
        {"row": 2, "column": "admission_number", "message": "Admission number already used"}
    ]
    assert job.ok_rows == 1


def test_unexpected_row_error_is_reported_and_job_continues(world, cloud, caplog):
    from apps.imports import students as importer

    real = importer.admit_student

    def boom(**kwargs):
        if kwargs["first_name"] == "Ada":
            raise RuntimeError("leak 08031234567")
        return real(**kwargs)

    with mock.patch.object(importer, "admit_student", side_effect=boom):
        job = run_job(world, csv_file([good_row(first_name="Ada"), good_row(first_name="Bola")]))
    assert job.status == ImportJob.Status.COMPLETED
    assert job.errors == [{"row": 2, "column": "", "message": "Unexpected error on this row"}]
    assert job.ok_rows == 1
    assert "08031234567" not in caplog.text


def test_plan_limit_reached_part_way(world, cloud, school_a):
    """Review Focus 4: earlier rows saved, later rows reported, job still completes."""
    plan = Plan.objects.create(
        code="import-tiny", name="Tiny", max_students=3, storage_bytes=10**9, sms_credits_per_term=0
    )
    old = school_a.plan
    school_a.plan = plan
    school_a.save()
    try:
        with schema_context("school_a"):
            StudentProfileFactory(enrolment__arm=world.a, enrolment__session=world.session)
        rows = [good_row(first_name=n) for n in ("Ada", "Bola", "Chi", "Dayo")]
        job = run_job(world, csv_file(rows))
        assert job.status == ImportJob.Status.COMPLETED
        assert (job.ok_rows, job.error_rows) == (2, 2)
        assert [e["row"] for e in job.errors] == [4, 5]
        assert {e["message"] for e in job.errors} == {"Plan limit reached (3 students)"}
        with schema_context("school_a"):
            assert set(Student.objects.values_list("first_name", flat=True)) >= {"Ada", "Bola"}
            assert not Student.objects.filter(first_name__in=["Chi", "Dayo"]).exists()
    finally:
        school_a.plan = old
        school_a.save()


def test_too_many_rows_fails_and_saves_nothing(world, cloud):
    rows = [good_row(first_name=f"S{i}") for i in range(2001)]
    job = run_job(world, csv_file(rows))
    assert job.status == ImportJob.Status.FAILED
    assert job.message == "File has more than 2,000 rows"
    with schema_context("school_a"):
        assert not Student.objects.exists()


def test_missing_columns_fail_the_job(world, cloud):
    job = run_job(world, csv_file([good_row()], headers=["first_name", "last_name"]))
    assert job.status == ImportJob.Status.FAILED
    assert "class" in job.message


def test_unexpected_job_failure_is_generic(world, cloud, caplog):
    with mock.patch(
        "apps.imports.tasks.import_students", side_effect=RuntimeError("secret 08031234567")
    ):
        job = run_job(world, csv_file([good_row()]))
    assert job.status == ImportJob.Status.FAILED
    assert "secret" not in job.message and job.message
    assert "08031234567" not in caplog.text
    assert job.finished_at is not None


def test_audits_counts_only(world, cloud):
    job = run_job(world, csv_file(five_rows()))
    with schema_context("school_a"):
        started = AuditEvent.objects.get(action="import.started", object_id=str(job.pk))
        done = AuditEvent.objects.get(action="import.completed", object_id=str(job.pk))
    keys = {"job_id", "kind", "total_rows", "ok_rows", "error_rows"}
    assert set(started.after) == keys and set(done.after) == keys
    assert done.after == {
        "job_id": job.pk,
        "kind": "students",
        "total_rows": 5,
        "ok_rows": 3,
        "error_rows": 2,
    }
    assert done.actor_id == world.admin.pk


def test_upload_is_deleted_after_completion_and_failure(
    world, cloud, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        job = run_job(world, csv_file([good_row()]))
    assert job.status == ImportJob.Status.COMPLETED
    assert cloud.store == {}
    with django_capture_on_commit_callbacks(execute=True):
        job = run_job(world, csv_file([good_row()], headers=["first_name"]))
    assert job.status == ImportJob.Status.FAILED
    assert cloud.store == {}


def test_job_runs_only_once(world, cloud):
    job = run_job(world, csv_file([good_row()]))
    with schema_context("school_a"):
        run_import.delay(job.pk)
        assert Student.objects.count() == 1


@pytest.mark.slow
def test_performance_500_rows_under_60_seconds(world, cloud, school_a):
    plan = Plan.objects.create(
        code="import-big",
        name="Big",
        max_students=None,
        storage_bytes=10**9,
        sms_credits_per_term=0,
    )
    old = school_a.plan
    school_a.plan = plan
    school_a.save()
    rows = [good_row(first_name=f"Pupil{i}") for i in range(500)]
    content = csv_file(rows)
    try:
        start = time.monotonic()
        job = run_job(world, content)
        elapsed = time.monotonic() - start
    finally:
        school_a.plan = old
        school_a.save()
    print(f"\n500-row import took {elapsed:.2f} s")
    assert job.status == ImportJob.Status.COMPLETED
    assert job.ok_rows == 500, job.errors[:3]
    assert elapsed < 60


# --- pages ---------------------------------------------------------------------


def admin_client(client_a, world):
    login_verified(client_a, world.admin)
    return client_a


def upload(name: str, content: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/octet-stream")


def test_upload_stores_privately_runs_and_shows_results(
    client_a, world, cloud, django_capture_on_commit_callbacks
):
    c = admin_client(client_a, world)
    with django_capture_on_commit_callbacks(execute=True):
        r = c.post(
            reverse("imports:new"),
            {"kind": "students", "file": upload("pupils.csv", csv_file(five_rows()))},
        )
    assert r.status_code == 302
    assert cloud.upload.call_args.kwargs["type"] == "authenticated"
    with schema_context("school_a"):
        job = ImportJob.objects.get()
        assert job.file_name.startswith("imports/") and job.file_name.endswith(".csv")
        assert job.created_by == world.admin
        assert job.status == ImportJob.Status.COMPLETED
    assert r["Location"] == reverse("imports:detail", args=[job.pk])
    page = c.get(r["Location"])
    body = page.content.decode()
    assert page.status_code == 200
    assert 'http-equiv="refresh"' not in body
    assert "JSS 9Z" in body
    assert reverse("imports:errors_csv", args=[job.pk]) in body
    assert f"{reverse('students:list')}?arm={world.a.pk}" in body
    assert f"{reverse('students:list')}?arm={world.b.pk}" in body


def test_errors_csv(client_a, world, cloud):
    job = run_job(world, csv_file(five_rows()))
    c = admin_client(client_a, world)
    r = c.get(reverse("imports:errors_csv", args=[job.pk]))
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/csv")
    lines = r.content.decode().splitlines()
    assert lines[0] == "row,column,message"
    assert lines[1].startswith("4,class,")
    assert lines[2].startswith("6,guardian_phone,")


def test_detail_refreshes_while_queued(client_a, world, cloud):
    with schema_context("school_a"):
        job = ImportJob.objects.create(
            kind="students", file_name="imports/x.csv", created_by=world.admin
        )
    c = admin_client(client_a, world)
    body = c.get(reverse("imports:detail", args=[job.pk])).content.decode()
    assert '<meta http-equiv="refresh" content="3">' in body


def test_failed_job_page_is_generic(client_a, world, cloud):
    with mock.patch(
        "apps.imports.tasks.import_students", side_effect=RuntimeError("secret detail")
    ):
        job = run_job(world, csv_file([good_row()]))
    c = admin_client(client_a, world)
    body = c.get(reverse("imports:detail", args=[job.pk])).content.decode()
    assert "secret detail" not in body
    assert job.message in body


def test_six_megabyte_file_rejected_before_storing(client_a, world, cloud):
    c = admin_client(client_a, world)
    big = b"first_name\r\n" + b"x" * (6 * 1024 * 1024)
    r = c.post(reverse("imports:new"), {"kind": "students", "file": upload("big.csv", big)})
    assert r.status_code == 200
    assert "5 MB" in r.content.decode()
    assert not cloud.upload.called
    with schema_context("school_a"):
        assert not ImportJob.objects.exists()


def test_xls_rejected(client_a, world, cloud):
    c = admin_client(client_a, world)
    r = c.post(reverse("imports:new"), {"kind": "students", "file": upload("old.xls", b"x")})
    assert r.status_code == 200
    assert ".csv or .xlsx" in r.content.decode()
    assert not cloud.upload.called


def test_storage_quota_shown_as_form_error(client_a, world, cloud, school_a):
    plan = Plan.objects.create(
        code="import-nostore",
        name="No storage",
        max_students=None,
        storage_bytes=10,
        sms_credits_per_term=0,
    )
    old = school_a.plan
    school_a.plan = plan
    school_a.save()
    try:
        c = admin_client(client_a, world)
        r = c.post(
            reverse("imports:new"),
            {"kind": "students", "file": upload("p.csv", csv_file([good_row()]))},
        )
        assert r.status_code == 200
        assert "Storage limit" in r.content.decode()
        with schema_context("school_a"):
            assert not ImportJob.objects.exists()
    finally:
        school_a.plan = old
        school_a.save()


def test_new_page_has_template_links(client_a, world):
    c = admin_client(client_a, world)
    body = c.get(reverse("imports:new")).content.decode()
    assert "imports/students_template.csv" in body
    assert "imports/students_template.xlsx" in body


def test_students_list_links_to_import(client_a, world):
    c = admin_client(client_a, world)
    body = c.get(reverse("students:list")).content.decode()
    assert reverse("imports:new") in body


def test_teacher_gets_403(client_a, world, cloud):
    job = run_job(world, csv_file([good_row()]))
    with schema_context("school_a"):
        teacher = StaffFactory()
        set_roles(teacher, ["teacher"], by=world.admin)
        client_a.force_login(teacher)
    for url in (
        reverse("imports:new"),
        reverse("imports:detail", args=[job.pk]),
        reverse("imports:errors_csv", args=[job.pk]),
    ):
        assert client_a.get(url).status_code == 403
    r = client_a.post(
        reverse("imports:new"), {"kind": "students", "file": upload("p.csv", b"first_name\r\n")}
    )
    assert r.status_code == 403


def test_template_files_have_exact_headers():
    import csv
    import io
    from pathlib import Path

    import openpyxl

    from apps.imports.students import TEMPLATE_HEADERS

    base = Path(__file__).resolve().parents[2] / "apps/imports/static/imports"
    text = (base / "students_template.csv").read_text(encoding="utf-8")
    assert list(csv.reader(io.StringIO(text))) == [list(TEMPLATE_HEADERS)]
    wb = openpyxl.load_workbook(base / "students_template.xlsx", read_only=True)
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    assert rows == [tuple(TEMPLATE_HEADERS)]
