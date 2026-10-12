"""Import pages (school admin only). Uploads are private and deleted once the job ends."""

import csv
import logging

from django.core.files.storage import default_storage
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.services import current_session
from apps.accounts.roles import SCHOOL_ADMIN, require_roles
from apps.core.storage import unique_upload_name
from apps.imports.forms import ImportForm
from apps.imports.models import ImportJob
from apps.imports.tasks import delete_upload, run_import
from apps.students.views import active_password_candidates, issue_and_render_sheet
from apps.tenancy.limits import PlanLimitExceeded

logger = logging.getLogger(__name__)

ERRORS_SHOWN = 200
RECENT_JOBS = 10


@never_cache
@require_roles(SCHOOL_ADMIN)
def new(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        kind = form.cleaned_data["kind"]
        if kind == ImportJob.Kind.STUDENTS and current_session() is None:
            form.add_error(None, "Set a current term before importing students.")
        else:
            upload = form.cleaned_data["file"]
            try:
                name = default_storage.save(unique_upload_name("imports", upload.name), upload)
            except PlanLimitExceeded as exc:
                form.add_error("file", exc.message)
            else:
                try:
                    job = ImportJob.objects.create(
                        kind=kind,
                        file_name=name,
                        original_name=(upload.name or "")[:255],
                        created_by=request.user,
                        started_verified=bool(
                            getattr(request.user, "is_verified", lambda: False)()
                        ),
                        base_url=request.build_absolute_uri("/").rstrip("/")[:255],
                    )
                except BaseException:
                    delete_upload(name)
                    raise
                transaction.on_commit(lambda: run_import.delay(job.pk))
                return redirect("imports:detail", pk=job.pk)
    jobs = ImportJob.objects.select_related("created_by")[:RECENT_JOBS]
    return render(request, "imports/new.html", {"form": form, "jobs": jobs})


@never_cache
@require_roles(SCHOOL_ADMIN)
def detail(request, pk: int):
    job = get_object_or_404(ImportJob.objects.select_related("created_by"), pk=pk)
    return render(
        request,
        "imports/detail.html",
        {
            "job": job,
            "errors": job.errors[:ERRORS_SHOWN],
            "more_errors": max(len(job.errors) - ERRORS_SHOWN, 0),
        },
    )


def _safe_cell(value: object) -> str:
    """Stop spreadsheet apps treating a cell as a formula (CSV injection)."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@never_cache
@require_roles(SCHOOL_ADMIN)
def errors_csv(request, pk: int):
    job = get_object_or_404(ImportJob, pk=pk)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="import-{job.pk}-errors.csv"'
    writer = csv.writer(response)
    writer.writerow(["row", "column", "message"])
    for entry in job.errors:
        writer.writerow(
            [
                entry.get("row", ""),
                _safe_cell(entry.get("column", "")),
                _safe_cell(entry.get("message", "")),
            ]
        )
    return response


@require_roles(SCHOOL_ADMIN)
@require_POST
def passwords(request, pk: int):
    """Print one-time passwords for the students this job admitted (and no one else).

    The ids come from the job, not the form, so any number of students fits in one POST.
    """
    job = get_object_or_404(ImportJob, pk=pk, kind=ImportJob.Kind.STUDENTS)
    ids = [i for i in job.admitted_student_ids if isinstance(i, int)]
    qs = active_password_candidates(request.user).filter(pk__in=ids)
    return issue_and_render_sheet(request, qs, back=reverse("imports:detail", args=[job.pk]))
