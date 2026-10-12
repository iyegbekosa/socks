"""Upload validation and job creation, shared by the HTML view and the API."""

import os
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction

from apps.academics.services import current_session
from apps.core.storage import unique_upload_name
from apps.imports.models import ImportJob
from apps.imports.parsing import ALLOWED_EXTENSIONS, MAX_BYTES
from apps.imports.tasks import delete_upload, run_import

ERRORS_SHOWN = 200  # errors listed on the HTML page and in the API
NO_TERM_MESSAGE = "Set a current term before importing students."


class ImportNotReady(Exception):
    """The school is not set up for this import. ``message`` is safe to show."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def validate_upload(upload: Any) -> None:
    """Raise ``ValidationError`` for a wrong extension or an oversized file."""
    ext = os.path.splitext(upload.name or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError("Upload a .csv or .xlsx file.")
    if upload.size > MAX_BYTES:
        raise ValidationError("The file is larger than 5 MB.")


def create_import_job(*, uploaded_file: Any, kind: str, user: Any, request: Any) -> ImportJob:
    """Store the (already validated) file privately, create the job and queue it.

    Validates the upload itself (``ValidationError``), so no caller can skip it.
    Raises ``ImportNotReady`` (no current term) or ``PlanLimitExceeded`` (storage quota).
    """
    validate_upload(uploaded_file)
    if kind == ImportJob.Kind.STUDENTS and current_session() is None:
        raise ImportNotReady(NO_TERM_MESSAGE)
    name = default_storage.save(unique_upload_name("imports", uploaded_file.name), uploaded_file)
    try:
        job = ImportJob.objects.create(
            kind=kind,
            file_name=name,
            original_name=(uploaded_file.name or "")[:255],
            created_by=user,
            started_verified=bool(getattr(user, "is_verified", lambda: False)()),
            base_url=request.build_absolute_uri("/").rstrip("/")[:255],
        )
    except BaseException:
        delete_upload(name)
        raise
    transaction.on_commit(lambda: run_import.delay(job.pk))
    return job
