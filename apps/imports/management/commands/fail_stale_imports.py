"""Fail import jobs whose worker died: they would otherwise stay queued/running forever."""

import logging
from datetime import timedelta

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.imports.models import ImportJob
from apps.imports.tasks import GENERIC_FAILURE, _audit

logger = logging.getLogger(__name__)


def _delete_upload(job: ImportJob) -> None:
    # The file name can carry the uploader's original file name: log the job id only.
    try:
        default_storage.delete(job.file_name)
    except Exception as exc:  # noqa: BLE001 - one undeletable file must not stop the sweep
        logger.error("Could not delete upload of import job %s: %s", job.pk, type(exc).__name__)


def fail_stale_jobs(older_than: timedelta) -> int:
    """Fail this schema's jobs queued or running for longer than ``older_than``.

    Never resumes a job. Returns how many were failed.
    """
    now = timezone.now()
    cutoff = now - older_than
    Status = ImportJob.Status
    stale = (
        Q(status=Status.QUEUED, created_at__lt=cutoff)
        | Q(status=Status.RUNNING, started_at__lt=cutoff)
        | Q(status=Status.RUNNING, started_at__isnull=True, created_at__lt=cutoff)
    )
    failed = 0
    for job in ImportJob.objects.filter(stale).select_related("created_by"):
        with transaction.atomic():
            # Conditional update: a job that finished meanwhile is left alone.
            changed = ImportJob.objects.filter(pk=job.pk, status=job.status).update(
                status=Status.FAILED, message=GENERIC_FAILURE, finished_at=now
            )
            if not changed:
                continue
            job.refresh_from_db()
            _audit("import.failed_stale", job)
        failed += 1
        _delete_upload(job)
    return failed


class Command(BaseCommand):
    help = (
        "Mark import jobs queued or running for too long as failed (every school), delete "
        "their uploads and audit the counts. Never resumes a job. Run it from cron."
    )

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=60)

    def handle(self, *args, **options):
        minutes = options["older_than_minutes"]
        if minutes < 1:
            self.stderr.write("--older-than-minutes must be at least 1.")
            raise SystemExit(2)
        total = 0
        schools = get_tenant_model().objects.exclude(schema_name=get_public_schema_name())
        for school in schools.order_by("schema_name"):
            with schema_context(school.schema_name):
                count = fail_stale_jobs(timedelta(minutes=minutes))
            if count:
                self.stdout.write(f"{school.schema_name}: failed {count} stale import job(s)")
            total += count
        self.stdout.write(f"Done: {total} stale import job(s) failed.")
