import logging

from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record
from apps.core.tasks import TenantTask
from apps.imports.models import ImportJob
from apps.imports.parsing import ImportFileError
from apps.imports.staff import import_staff
from apps.imports.students import import_students
from config.celery import app

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "The import could not be completed. Check the file and try again."


def _audit(action: str, job: ImportJob) -> None:
    record(
        action,
        actor=job.created_by,
        obj=job,
        after={
            "job_id": job.pk,
            "kind": job.kind,
            "total_rows": job.total_rows,
            "ok_rows": job.ok_rows,
            "error_rows": job.error_rows,
        },
    )


def delete_upload(name: str) -> None:
    """The upload holds children's data: remove it once the job is done. Never raises."""
    try:
        default_storage.delete(name)
    except Exception:  # noqa: BLE001
        logger.exception("Could not delete import upload %s", name)


@app.task(base=TenantTask, queue="default")
def run_import(job_id: int) -> None:
    """queued → running → completed | failed. A job already claimed is left alone."""
    claimed = ImportJob.objects.filter(pk=job_id, status=ImportJob.Status.QUEUED).update(
        status=ImportJob.Status.RUNNING, started_at=timezone.now()
    )
    if not claimed:
        return
    job = ImportJob.objects.select_related("created_by").get(pk=job_id)
    try:
        _audit("import.started", job)
        if job.kind == ImportJob.Kind.STUDENTS:
            import_students(job)
        elif job.kind == ImportJob.Kind.STAFF:
            import_staff(job, verified=job.started_verified)
        else:
            raise ImportFileError("This kind of import is not available yet.")
    except ImportFileError as exc:
        job.status = ImportJob.Status.FAILED
        job.message = exc.message[:200]
    except Exception as exc:  # noqa: BLE001 - the job must end in a final state
        # Type name only: the exception text could quote file contents.
        logger.error("Import job %s failed: %s", job.pk, type(exc).__name__)
        job.status = ImportJob.Status.FAILED
        job.message = GENERIC_FAILURE
    else:
        job.status = ImportJob.Status.COMPLETED
    job.finished_at = timezone.now()
    # Conditional: if fail_stale_imports already failed this job, the worker leaves it failed.
    saved = ImportJob.objects.filter(pk=job.pk, status=ImportJob.Status.RUNNING).update(
        status=job.status, message=job.message, finished_at=job.finished_at
    )
    if saved and job.status == ImportJob.Status.COMPLETED:
        _audit("import.completed", job)
    name = job.file_name
    transaction.on_commit(lambda: delete_upload(name))
