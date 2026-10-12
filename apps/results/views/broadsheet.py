"""The class broadsheet (HTML and CSV): every ranked student's subject totals, total, average
and position, with each subject's highest, lowest and average below.

It reads the stored ``ResultSummary`` and ``SubjectResult`` rows only (never recomputes), and
only once the class is approved (and computed) or published. Visible to the school admin,
principal, proprietor and the arm's form teacher for the term's session.
"""

import csv
import io
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.academics.models import ClassArm, Term
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, TEACHER, require_roles
from apps.core.parsing import parse_id
from apps.results.compute import ordinal, stats
from apps.results.csvsheet import safe_cell
from apps.results.models import ClassResult, ResultSummary, SubjectResult
from apps.results.workflow import is_form_teacher

NOT_READY = "Results not ready"
VISIBLE = (ClassResult.Status.APPROVED, ClassResult.Status.PUBLISHED)


def _arm_and_term(request, arm_pk: str, term_pk: str) -> tuple[ClassArm, Term]:
    arm_id, term_id = parse_id(str(arm_pk)), parse_id(str(term_pk))
    if not arm_id or not term_id:
        raise Http404
    arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=arm_id)
    term = get_object_or_404(Term.objects.select_related("session"), pk=term_id)
    user = request.user
    if not (user.has_role(PRINCIPAL, SCHOOL_ADMIN, PROPRIETOR) or is_form_teacher(user, arm, term)):
        raise PermissionDenied
    return arm, term


def broadsheet_data(arm: ClassArm, term: Term) -> dict[str, Any] | None:
    """The broadsheet from the stored results, or None when they are not ready to show."""
    klass = ClassResult.objects.filter(arm=arm, term=term).first()
    if klass is None or klass.status not in VISIBLE or klass.compute_error:
        return None
    summaries = list(
        ResultSummary.objects.filter(arm=arm, term=term)
        .select_related("student")
        .order_by("position", "student__last_name", "student__first_name", "student_id")
    )
    if not summaries:
        return None
    student_ids = [s.student_id for s in summaries]
    results = list(
        SubjectResult.objects.filter(
            term=term,
            offering__arm=arm,
            offering__session_id=term.session_id,
            student_id__in=student_ids,
        )
        .order_by("subject_name", "offering_id")
        .values_list("offering_id", "subject_name", "student_id", "total")
    )
    subjects: list[dict[str, Any]] = []
    column: dict[int, int] = {}
    cells: dict[tuple[int, int], Decimal] = {}
    for offering_id, subject_name, student_id, total in results:
        if offering_id not in column:
            column[offering_id] = len(subjects)
            subjects.append({"offering_id": offering_id, "name": subject_name})
        cells[(student_id, offering_id)] = total
    rows: list[dict[str, Any]] = []
    for s in summaries:
        rows.append(
            {
                "student_id": s.student_id,
                "admission_number": s.student.admission_number or "",
                "name": s.student.full_name,
                "totals": [cells.get((s.student_id, sub["offering_id"])) for sub in subjects],
                "total": s.total,
                "average": s.average,
                "position": ordinal(s.position),
            }
        )
    highest: list[Decimal | None] = []
    lowest: list[Decimal | None] = []
    average: list[Decimal | None] = []
    for i in range(len(subjects)):
        shown = [row["totals"][i] for row in rows if row["totals"][i] is not None]
        hi, lo, avg = stats(shown) if shown else (None, None, None)
        highest.append(hi)
        lowest.append(lo)
        average.append(avg)
    return {
        "klass": klass,
        "subjects": subjects,
        "rows": rows,
        "footer": [
            {"label": "Highest", "values": highest},
            {"label": "Lowest", "values": lowest},
            {"label": "Average", "values": average},
        ],
        "out_of": summaries[0].out_of,
    }


@never_cache
@require_GET
@require_roles(TEACHER, PRINCIPAL, SCHOOL_ADMIN, PROPRIETOR)
def broadsheet(request, arm_pk: str, term_pk: str):
    arm, term = _arm_and_term(request, arm_pk, term_pk)
    data = broadsheet_data(arm, term)
    ctx: dict[str, Any] = {"arm": arm, "term": term, "ready": data is not None}
    if data is not None:
        ctx.update(data)
    return render(request, "results/broadsheet.html", ctx)


def _csv(data: dict[str, Any]) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        ["admission_number", "name", *(safe_cell(s["name"]) for s in data["subjects"])]
        + ["total", "average", "position"]
    )
    for row in data["rows"]:
        writer.writerow(
            [safe_cell(row["admission_number"]), safe_cell(row["name"])]
            + [safe_cell(t) for t in row["totals"]]
            + [safe_cell(row["total"]), safe_cell(row["average"]), safe_cell(row["position"])]
        )
    for line in data["footer"]:
        writer.writerow([line["label"], ""] + [safe_cell(v) for v in line["values"]])
    return out.getvalue()


@require_GET
@require_roles(TEACHER, PRINCIPAL, SCHOOL_ADMIN, PROPRIETOR)
def broadsheet_csv(request, arm_pk: str, term_pk: str):
    arm, term = _arm_and_term(request, arm_pk, term_pk)
    data = broadsheet_data(arm, term)
    if data is None:
        response = HttpResponse(NOT_READY, status=409, content_type="text/plain; charset=utf-8")
        response["Cache-Control"] = "no-store"
        return response
    response = HttpResponse(_csv(data), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="broadsheet-{arm.pk}-{term.pk}.csv"'
    response["Cache-Control"] = "no-store"
    return response
