import io
import logging
from datetime import timedelta
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.core.storage import public_id_for, unique_upload_name
from apps.imports.models import ImportJob
from apps.imports.tasks import GENERIC_FAILURE, run_import
from tests.accounts.factories import StaffFactory

pytestmark = pytest.mark.django_db


def _job(world, status, *, age_minutes, started_minutes=None, name="kid-names.csv"):
    file_name = default_storage.save(unique_upload_name("imports", name), ContentFile(b"x"))
    job = ImportJob.objects.create(
        kind="students", status=status, file_name=file_name, created_by=world.admin
    )
    now = timezone.now()
    started = now - timedelta(minutes=started_minutes) if started_minutes is not None else None
    ImportJob.objects.filter(pk=job.pk).update(
        created_at=now - timedelta(minutes=age_minutes), started_at=started
    )
    job.refresh_from_db()
    return job


def _run(*args) -> str:
    out = io.StringIO()
    call_command("fail_stale_imports", *args, stdout=out)
    return out.getvalue()


def test_fails_only_stale_jobs_deletes_uploads_and_audits_counts(world, cloud):
    with schema_context("school_a"):
        old_queued = _job(world, "queued", age_minutes=90)
        old_running = _job(world, "running", age_minutes=120, started_minutes=70)
        fresh_queued = _job(world, "queued", age_minutes=10)
        fresh_running = _job(world, "running", age_minutes=80, started_minutes=5)
        done = _job(world, "completed", age_minutes=500)
    output = _run()
    assert "school_a: failed 2" in output
    with schema_context("school_a"):
        for job in (old_queued, old_running):
            job.refresh_from_db()
            assert job.status == "failed"
            assert job.message == GENERIC_FAILURE
            assert job.finished_at is not None
            assert public_id_for(job.file_name) not in cloud.store
        for job, status in (
            (fresh_queued, "queued"),
            (fresh_running, "running"),
            (done, "completed"),
        ):
            job.refresh_from_db()
            assert job.status == status
        events = AuditEvent.objects.filter(action="import.failed_stale")
        assert {e.object_id for e in events} == {str(old_queued.pk), str(old_running.pk)}
        for event in events:
            assert set(event.after) == {"job_id", "kind", "total_rows", "ok_rows", "error_rows"}
    assert cloud.destroy.call_count == 2


def test_threshold_option(world, cloud):
    with schema_context("school_a"):
        job = _job(world, "queued", age_minutes=20)
    _run()
    with schema_context("school_a"):
        job.refresh_from_db()
        assert job.status == "queued"
    _run("--older-than-minutes", "15")
    with schema_context("school_a"):
        job.refresh_from_db()
        assert job.status == "failed"


def test_never_resumes_a_failed_job(world, cloud):
    with schema_context("school_a"):
        job = _job(world, "queued", age_minutes=90)
    _run()
    with schema_context("school_a"), mock.patch("apps.imports.tasks.import_students") as imp:
        run_import.delay(job.pk)
        imp.assert_not_called()
        job.refresh_from_db()
        assert job.status == "failed"


def test_covers_every_school(world, cloud, school_b):
    with schema_context("school_b"):
        admin_b = StaffFactory()
        set_roles(admin_b, ["school_admin"], by=admin_b)
        other = ImportJob.objects.create(
            kind="staff", file_name="imports/b.csv", created_by=admin_b
        )
        ImportJob.objects.filter(pk=other.pk).update(created_at=timezone.now() - timedelta(hours=3))
    try:
        assert "school_b: failed 1" in _run()
        with schema_context("school_b"):
            other.refresh_from_db()
            assert other.status == "failed"
    finally:
        with schema_context("school_b"):
            ImportJob.objects.filter(pk=other.pk).delete()


def test_delete_failure_is_logged_without_file_name(world, cloud, caplog):
    with schema_context("school_a"):
        job = _job(world, "queued", age_minutes=90, name="Adaeze-Okafor.csv")
    cloud.destroy.side_effect = RuntimeError("boom Adaeze-Okafor.csv")
    with caplog.at_level(logging.ERROR):
        _run()
    with schema_context("school_a"):
        job.refresh_from_db()
        assert job.status == "failed"
    assert f"import job {job.pk}: RuntimeError" in caplog.text
    assert "Adaeze" not in caplog.text


def test_worker_finishing_after_stale_sweep_leaves_the_job_failed(world, cloud):
    with schema_context("school_a"):
        job = _job(world, "queued", age_minutes=1)

        def sweep_during_import(running_job):
            # The sweeper fails the job while the worker is still importing.
            ImportJob.objects.filter(pk=running_job.pk).update(
                status="failed", message=GENERIC_FAILURE
            )

        with mock.patch("apps.imports.tasks.import_students", side_effect=sweep_during_import):
            run_import.delay(job.pk)
        job.refresh_from_db()
        assert job.status == "failed"
        assert job.message == GENERIC_FAILURE
        assert not AuditEvent.objects.filter(
            action="import.completed", object_id=str(job.pk)
        ).exists()
