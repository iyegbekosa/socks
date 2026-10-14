"""Attendance views: a class's term grid, the whole-school day view and a student's own."""

import csv
import io
from datetime import date
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache

from apps.academics.models import ClassArm, Term
from apps.academics.services import current_term
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles
from apps.attendance import register as reg
from apps.attendance.days import is_school_day, school_days
from apps.attendance.models import AttendanceRecord
from apps.attendance.totals import Totals, term_totals
from apps.core.parsing import parse_id
from apps.results.csvsheet import safe_cell
from apps.students.models import Enrolment, Student

Status = AttendanceRecord.Status
LETTERS = {
    Status.PRESENT: "P",
    Status.ABSENT: "A",
    Status.LATE: "L",
    Status.EXCUSED: "E",
}
NONE = "·"
MOVED = "↗"
DASH = "—"
SCHOOL_ROLES = (PRINCIPAL, SCHOOL_ADMIN, PROPRIETOR)
CSV_TOTALS = ["Present", "Opened", "Late", "Absent", "Excused"]


def _class_access(user, arm: ClassArm, term: Term | None) -> bool:
    """Form teacher of the arm for the term's session, principal, admin or proprietor.

    Returns whether the user may take the arm's registers (the proprietor only reads)."""
    if reg.access(user, arm, term) is not None:
        return True
    if user.is_active and getattr(user, "kind", None) == "staff" and user.has_role(PROPRIETOR):
        return False
    raise PermissionDenied


def _pick_term(request, current: Term) -> tuple[Term, list[Term]]:
    terms = list(Term.objects.filter(session_id=current.session_id).order_by("number"))
    raw = request.GET.get("term")
    pk = parse_id(raw) if raw else None
    chosen = next((t for t in terms if t.pk == pk), current)
    return chosen, terms


def _totals(recs) -> Totals:
    """``term_totals`` for one student from their already-fetched records (one per date, all
    arms, none dated after today), saving a query."""
    statuses = [r["status"] for r in recs]
    return Totals(
        days_opened=len(statuses),
        days_present=sum(x in (Status.PRESENT, Status.LATE) for x in statuses),
        times_late=statuses.count(Status.LATE),
        times_absent=statuses.count(Status.ABSENT),
        times_excused=statuses.count(Status.EXCUSED),
    )


def _cells(days, by_date, arm_pk):
    cells = []
    for d in days:
        rec = by_date.get(d)
        if rec is None:
            cells.append({"date": d, "letter": NONE, "title": ""})
        elif rec["arm_id"] == arm_pk:
            cells.append({"date": d, "letter": LETTERS[rec["status"]], "title": ""})
        else:
            cells.append({"date": d, "letter": MOVED, "title": rec["arm_name"]})
    return cells


@login_required
def class_page(request, arm_id):
    pk = parse_id(arm_id)
    if pk is None:
        raise Http404
    arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=pk)
    cur = current_term()
    if cur is None:
        _class_access(request.user, arm, None)
        response = render(request, "attendance/class.html", {"arm": arm, "term": None})
        add_never_cache_headers(response)
        return response
    term, terms = _pick_term(request, cur)
    # Registers can be taken only in the current term, so only its days link to them.
    can_take = _class_access(request.user, arm, term) and term.pk == cur.pk

    until = min(timezone.localdate(), term.end_date)
    days = school_days(term, until=until)
    listed = reg.students_for(arm, until, term=term)
    listed_ids = [s.pk for s in listed]
    # Every record in the date range taken in this arm, plus the listed students' records
    # taken elsewhere (a student who moved, listed or not): one query.
    in_arm = AttendanceRecord.objects.filter(
        arm=arm, date__gte=term.start_date, date__lte=until
    ).values("student_id")
    recs = list(
        AttendanceRecord.objects.filter(date__gte=term.start_date, date__lte=until)
        .filter(Q(arm=arm) | Q(student_id__in=listed_ids) | Q(student_id__in=in_arm))
        .values("student_id", "date", "status", "arm_id", "arm__level__name", "arm__suffix")
        .order_by()
    )
    by_student: dict[int, dict[date, dict[str, Any]]] = {}
    arm_dates: set[date] = set()
    for r in recs:
        sep = "" if len(r["arm__suffix"]) == 1 else " "
        r["arm_name"] = f"{r['arm__level__name']}{sep}{r['arm__suffix']}"
        by_student.setdefault(r["student_id"], {})[r["date"]] = r
        if r["arm_id"] == arm.pk:
            arm_dates.add(r["date"])
    listed_set = set(listed_ids)
    extra_ids = [sid for sid in by_student if sid not in listed_set]
    students = listed
    if extra_ids:
        extra = Student.objects.filter(pk__in=extra_ids).select_related("user")
        students = sorted([*listed, *extra], key=lambda s: (s.last_name, s.first_name, s.pk))
    totals = {sid: _totals(by_date.values()) for sid, by_date in by_student.items()}
    rows = [
        {
            "student": s,
            "cells": _cells(days, by_student.get(s.pk, {}), arm.pk),
            "totals": totals.get(s.pk),
        }
        for s in students
    ]
    missing = [d for d in days if d not in arm_dates]
    if request.GET.get("format") == "csv":
        return _csv(arm, term, days, rows)
    ctx = {
        "arm": arm,
        "term": term,
        "terms": terms,
        "days": days,
        "missing": missing,
        "missing_set": set(missing),
        "rows": rows,
        "can_take": can_take,
    }
    response = render(request, "attendance/class.html", ctx)
    add_never_cache_headers(response)
    return response


def _csv(arm: ClassArm, term: Term, days: list[date], rows: list[dict[str, Any]]) -> HttpResponse:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Admission no.", "Name", *[d.isoformat() for d in days], *CSV_TOTALS])
    for row in rows:
        s, t = row["student"], row["totals"]
        letters = ["" if c["letter"] in (NONE, MOVED) else c["letter"] for c in row["cells"]]
        nums = (
            [0, 0, 0, 0, 0]
            if t is None
            else [t.days_present, t.days_opened, t.times_late, t.times_absent, t.times_excused]
        )
        writer.writerow(
            [safe_cell(s.admission_number or ""), safe_cell(s.full_name), *letters, *nums]
        )
    response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="attendance-{arm.pk}-{term.pk}.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _percent(present: int, opened: int) -> str:
    return f"{present * 100 / opened:.1f}" if opened else DASH


@never_cache
@require_roles(*SCHOOL_ROLES)
def school_page(request):
    today = timezone.localdate()
    day = today
    error = ""
    raw = request.GET.get("date")
    if raw:
        parsed = reg.parse_iso_date(raw)
        if parsed is None:
            error = "Enter a valid date as YYYY-MM-DD."
        else:
            day = parsed
    ctx: dict[str, Any] = {"day": day, "error": error, "rows": []}
    if day > today:
        ctx["future"] = True
        return render(request, "attendance/school.html", ctx)
    term = Term.objects.filter(start_date__lte=day, end_date__gte=day).first()
    if term is None or not is_school_day(term, day):
        ctx["not_school_day"] = True
        return render(request, "attendance/school.html", ctx)
    present = Q(status__in=[Status.PRESENT, Status.LATE])
    today_counts = {
        r["arm_id"]: r
        for r in AttendanceRecord.objects.filter(date=day)
        .values("arm_id")
        .annotate(
            n=Count("pk"),
            present=Count("pk", filter=present),
            absent=Count("pk", filter=Q(status=Status.ABSENT)),
            late=Count("pk", filter=Q(status=Status.LATE)),
        )
        .order_by()
    }
    to_date = {
        r["arm_id"]: r
        for r in AttendanceRecord.objects.filter(date__gte=term.start_date, date__lte=day)
        .values("arm_id")
        .annotate(opened=Count("pk"), present=Count("pk", filter=present))
        .order_by()
    }
    arms = (
        ClassArm.objects.filter(
            enrolments__session_id=term.session_id,
            enrolments__status=Enrolment.Status.ACTIVE,
            enrolments__student__status=Student.Status.ACTIVE,
        )
        .select_related("level")
        .distinct()
    )
    rows = []
    for arm in arms:
        c = today_counts.get(arm.pk)
        t = to_date.get(arm.pk)
        rows.append(
            {
                "arm": arm,
                "registered": c["n"] if c else 0,
                "present": c["present"] if c else 0,
                "absent": c["absent"] if c else 0,
                "late": c["late"] if c else 0,
                "percent": _percent(t["present"], t["opened"]) if t else DASH,
            }
        )
    rows.sort(key=lambda r: r["registered"] > 0)  # stable: unregistered first, then level order
    ctx.update(rows=rows, term=term)
    return render(request, "attendance/school.html", ctx)


@never_cache
@login_required
def my_attendance(request):
    if request.user.kind != "student":
        raise PermissionDenied
    term = current_term()
    ctx: dict[str, Any] = {"term": term}
    student = Student.objects.filter(user=request.user).first()
    if term is not None and student is not None:
        ctx["totals"] = term_totals([student.pk], term).get(student.pk)
        recs = AttendanceRecord.objects.filter(
            student=student,
            date__gte=term.start_date,
            date__lte=term.end_date,
            status__in=[Status.ABSENT, Status.LATE],
        ).values_list("date", "status")
        ctx["absent_dates"] = [d for d, s in recs if s == Status.ABSENT]
        ctx["late_dates"] = [d for d, s in recs if s == Status.LATE]
    return render(request, "attendance/my_attendance.html", ctx)
