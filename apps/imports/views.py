"""Import pages (school admin only). Uploads are private and deleted once the job ends."""

import csv
import logging
from typing import Any

from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache

from apps.accounts.roles import SCHOOL_ADMIN, require_roles
from apps.imports.forms import ImportForm
from apps.imports.models import ImportJob
from apps.imports.services import ERRORS_SHOWN, ImportNotReady, create_import_job
from apps.students.models import Enrolment
from apps.students.views import active_password_candidates, issue_and_render_sheet
from apps.tenancy.limits import PlanLimitExceeded

logger = logging.getLogger(__name__)

RECENT_JOBS = 10
PASSWORD_BATCH_SIZE = 200


@never_cache
@require_roles(SCHOOL_ADMIN)
def new(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            job = create_import_job(
                uploaded_file=form.cleaned_data["file"],
                kind=form.cleaned_data["kind"],
                user=request.user,
                request=request,
            )
        except ImportNotReady as exc:
            form.add_error(None, exc.message)
        except PlanLimitExceeded as exc:
            form.add_error("file", exc.message)
        else:
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
            "password_batches": password_batches(job)
            if job.kind == ImportJob.Kind.STUDENTS
            else [],
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


def _printed_on(job: ImportJob) -> dict[tuple[int, int], str]:
    """(arm_id, batch) → latest print time as DD/MM/YYYY HH:MM (local time)."""
    out: dict[tuple[int, int], str] = {}
    for entry in job.printed_batches:
        try:
            key = (int(entry["arm_id"]), int(entry["batch"]))
            at = parse_datetime(str(entry["at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if at is not None:
            out[key] = timezone.localtime(at).strftime("%d/%m/%Y %H:%M")
    return out


def password_batches(job: ImportJob) -> list[dict[str, Any]]:
    """The job's admitted students grouped by the class they were imported into, split
    into batches of at most ``PASSWORD_BATCH_SIZE`` (Argon2 makes bigger sheets too slow
    for one request). Batches are ordered by student pk, so they stay stable over time."""
    ids = [i for i in job.admitted_student_ids if isinstance(i, int)]
    if not ids:
        return []
    # The admission enrolment is each student's earliest one; later moves don't regroup.
    first_arm: dict[int, int] = {}
    arm_names: dict[int, str] = {}
    rows = (
        Enrolment.objects.filter(student_id__in=ids)
        .select_related("arm__level")
        .order_by("session__start_date", "pk")
    )
    for enrolment in rows:
        if enrolment.student_id not in first_arm:
            first_arm[enrolment.student_id] = enrolment.arm_id
            arm_names[enrolment.arm_id] = enrolment.arm.display_name
    by_arm: dict[int, list[int]] = {}
    for student_id in sorted(first_arm):
        by_arm.setdefault(first_arm[student_id], []).append(student_id)
    printed = _printed_on(job)
    batches = []
    for arm_id in sorted(by_arm, key=lambda a: (arm_names[a], a)):
        members = by_arm[arm_id]
        split = len(members) > PASSWORD_BATCH_SIZE
        for start in range(0, len(members), PASSWORD_BATCH_SIZE):
            chunk = members[start : start + PASSWORD_BATCH_SIZE]
            batch = start // PASSWORD_BATCH_SIZE + 1
            label = arm_names[arm_id]
            if split:
                label = f"{label} ({start + 1}\u2013{start + len(chunk)})"
            batches.append(
                {
                    "arm_id": arm_id,
                    "batch": batch,
                    "label": label,
                    "ids": chunk,
                    "count": len(chunk),
                    "printed_on": printed.get((arm_id, batch), ""),
                }
            )
    return batches


@require_roles(SCHOOL_ADMIN)
def passwords_confirm(request, pk: int, arm_id: int, batch: int):
    """GET asks for confirmation (issues nothing); POST issues one batch's one-time
    passwords and renders the sheet. Only students this job admitted are ever included."""
    job = get_object_or_404(ImportJob, pk=pk, kind=ImportJob.Kind.STUDENTS)
    chosen = next(
        (b for b in password_batches(job) if b["arm_id"] == arm_id and b["batch"] == batch),
        None,
    )
    if chosen is None:
        raise Http404
    qs = active_password_candidates(request.user).filter(pk__in=chosen["ids"])
    back = reverse("imports:detail", args=[job.pk])
    if request.method != "POST":
        page = render(
            request,
            "imports/passwords_confirm.html",
            {"job": job, "batch": chosen, "count": qs.count(), "back": back},
        )
        page["Cache-Control"] = "no-store"  # the sheet sets the same header
        return page
    with transaction.atomic():
        response = issue_and_render_sheet(request, qs, back=back)
        if response.status_code == 200:
            locked = ImportJob.objects.select_for_update().get(pk=job.pk)
            locked.printed_batches = [
                *locked.printed_batches,
                {"arm_id": arm_id, "batch": batch, "at": timezone.now().isoformat()},
            ]
            locked.save(update_fields=["printed_batches"])
    return response
