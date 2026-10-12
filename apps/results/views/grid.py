from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.academics.models import FormTeacher, SubjectOffering, Term
from apps.academics.services import current_session, current_term
from apps.accounts.roles import TEACHER, require_roles
from apps.core.parsing import parse_id
from apps.results import csvsheet
from apps.results.models import ClassResult, ScoreSheet
from apps.results.scope import can_enter_scores
from apps.results.scores import (
    ResultsLocked,
    SheetData,
    VersionConflict,
    save_cells,
    sheet_rows,
)

NO_TERM = "Set a current term first"
WRONG_SESSION = "This subject is not in the current term's session."


def _offering_or_403(request, offering_pk) -> SubjectOffering:
    pk = parse_id(str(offering_pk))
    offering = get_object_or_404(
        SubjectOffering.objects.select_related("subject", "arm__level", "session"),
        pk=pk if pk is not None else 0,
    )
    if not can_enter_scores(request.user, offering):
        raise PermissionDenied
    return offering


def _sheet(offering) -> tuple[Term | None, SheetData | None, str | None]:
    term = current_term()
    if term is None:
        return None, None, NO_TERM
    if term.session_id != offering.session_id:
        return term, None, WRONG_SESSION
    data = sheet_rows(offering, term)
    for row in data.rows:
        vals = [Decimal(c["value"]) for c in row["cells"] if c["value"] is not None]
        row["total"] = sum(vals, Decimal("0")).quantize(Decimal("0.1")) if vals else None
    return term, data, None


def _colspan(data: SheetData | None) -> int:
    # name + components + total + status
    return 3 + (len(data.components) if data else 0)


@never_cache
@require_roles(TEACHER)
def my_sheets(request):
    term = current_term()
    session = current_session()
    rows = []
    form_arms = []
    if session is not None:
        form_arms = [
            f.arm
            for f in FormTeacher.objects.filter(teacher=request.user, session=session)
            .select_related("arm__level")
            .order_by("arm__level__order", "arm__suffix")
        ]
    if session is not None:
        offerings = SubjectOffering.objects.filter(
            teacher=request.user, session=session
        ).select_related("subject", "arm__level")
        sheets: dict[int, str] = {}
        classes: dict[int, str] = {}
        if term is not None:
            sheets = dict(ScoreSheet.objects.filter(term=term).values_list("offering_id", "status"))
            classes = dict(ClassResult.objects.filter(term=term).values_list("arm_id", "status"))
        rows = [
            {
                "offering": o,
                "sheet_status": sheets.get(o.pk, "draft"),
                "class_status": classes.get(o.arm_id, "draft"),
            }
            for o in offerings.order_by("arm__level__order", "arm__suffix", "subject__name")
        ]
    return render(
        request, "results/my_sheets.html", {"rows": rows, "term": term, "form_arms": form_arms}
    )


@never_cache
@require_roles(TEACHER)
def grid(request, offering_pk):
    offering = _offering_or_403(request, offering_pk)
    term, data, problem = _sheet(offering)
    return render(
        request,
        "results/grid.html",
        {
            "offering": offering,
            "term": term,
            "data": data,
            "problem": problem,
            "colspan": _colspan(data),
        },
    )


@never_cache
@require_GET
@require_roles(TEACHER)
def grid_csv(request, offering_pk):
    offering = _offering_or_403(request, offering_pk)
    term, data, problem = _sheet(offering)
    if data is None or term is None:
        return HttpResponse(problem, status=409, content_type="text/plain; charset=utf-8")
    name = slugify(
        f"{offering.subject.name}-{offering.arm.display_name}-{term.get_number_display()}"
    )
    response = HttpResponse(csvsheet.sheet_to_csv(data), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name or "scores"}.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _upload_errors(request, offering) -> list[str]:
    term, data, problem = _sheet(offering)
    if data is None or term is None:
        return [problem or NO_TERM]
    upload = request.FILES.get("file")
    if upload is None:
        return ["Choose a CSV file to upload."]
    if not upload.name.lower().endswith(".csv"):
        return ["Upload a .csv file."]
    if upload.size > csvsheet.MAX_BYTES:
        return ["The file is larger than 1 MB."]
    parsed = csvsheet.parse_upload(data, upload.read())
    if parsed.errors:
        return parsed.errors
    try:
        # Not wrapped in atomic(): a locked refusal must keep its results.edit_refused audit.
        result = save_cells(offering, term, parsed.cells, by=request.user, request=request)
    except ResultsLocked as exc:
        return [exc.message]
    except VersionConflict:
        return ["Someone changed scores while you were uploading — try again"]
    except ValidationError as exc:
        return csvsheet.describe_validation_error(data, parsed, exc)
    messages.success(request, f"{result.changed} cells saved")
    return []


@never_cache
@require_POST
@require_roles(TEACHER)
def grid_upload(request, offering_pk):
    offering = _offering_or_403(request, offering_pk)
    errors = _upload_errors(request, offering)
    if not errors:
        return redirect("results:grid", offering_pk=offering.pk)
    term, data, problem = _sheet(offering)
    shown = errors[:50]
    return render(
        request,
        "results/grid.html",
        {
            "offering": offering,
            "term": term,
            "data": data,
            "problem": problem,
            "colspan": _colspan(data),
            "upload_errors": shown,
            "more_errors": len(errors) - len(shown),
        },
        status=400,
    )
