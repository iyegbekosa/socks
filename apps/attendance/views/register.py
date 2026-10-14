import re
from datetime import date
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import dateformat, timezone
from django.views.decorators.cache import never_cache

from apps.academics.models import ClassArm, Term
from apps.academics.services import current_term
from apps.attendance import register as reg
from apps.attendance.days import RegisterDateError, check_register_date
from apps.core.parsing import parse_id

FIELD = re.compile(r"s_(\d{1,18})")


def parse_day(raw: str) -> date:
    """An ISO ``YYYY-MM-DD`` date from the URL, else 404."""
    d = reg.parse_iso_date(raw)
    if d is None:
        raise Http404
    return d


def get_arm(raw: str) -> ClassArm:
    pk = parse_id(raw)
    if pk is None:
        raise Http404
    return get_object_or_404(ClassArm.objects.select_related("level"), pk=pk)


def _statuses(post: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    for key, value in post.items():
        if key.startswith("s_"):
            m = FIELD.fullmatch(key)
            if m is None:
                raise ValidationError({"records": ["Some students are not on this register."]})
            out[int(m.group(1))] = value
    return out


def _access(request: Any, arm: ClassArm) -> tuple[Term | None, str]:
    term = current_term()
    # The role check is access() rather than require_roles, so roles are read once: only the
    # arm's form teacher, the principal and the school admin get in.
    level = reg.access(request.user, arm, term)
    if level is None:
        raise PermissionDenied
    return term, level


@never_cache
@login_required
def today(request, arm_id):
    arm = get_arm(arm_id)
    _access(request, arm)
    return redirect("attendance:register", arm_id=arm.pk, day=timezone.localdate().isoformat())


@never_cache
@login_required
def register_page(request, arm_id, day):
    arm = get_arm(arm_id)
    d = parse_day(day)
    term, level = _access(request, arm)
    ctx: dict[str, Any] = {"arm": arm, "day": d}
    if request.method == "POST":
        return _save(request, ctx, term, level)
    try:
        term = check_register_date(d, term=term)
    except RegisterDateError as exc:
        return _date_error(request, ctx, exc)
    locked = reg.is_locked(arm, term)
    editable = not locked and reg.editable_by(level, d, term)
    return _page(request, ctx, term, editable=editable, locked=locked)


def _date_error(request: Any, ctx: dict[str, Any], exc: RegisterDateError) -> HttpResponse:
    ctx["date_error"] = exc.message
    return render(request, "attendance/register.html", ctx, status=400)


def _save(request: Any, ctx: dict[str, Any], term: Term | None, level: str) -> HttpResponse:
    arm, d = ctx["arm"], ctx["day"]
    try:
        if not reg.editable_by(level, d, term):
            # Say why, in the API's order: a bad date (400), locked (409), else forbidden.
            term = check_register_date(d, term=term)
            if reg.is_locked(arm, term):
                raise reg.RegisterLocked()
            raise PermissionDenied
        posted = _statuses(request.POST)
        reg.save_register(arm, d, posted, by=request.user, request=request, term=term)
    except RegisterDateError as exc:
        return _date_error(request, ctx, exc)
    except reg.RegisterLocked:
        assert term is not None
        return _page(request, ctx, term, editable=False, locked=True, status=409)
    except ValidationError as exc:
        assert term is not None
        ctx["errors"] = exc.messages
        return _page(request, ctx, term, editable=True, locked=False, status=400)
    messages.success(request, f"Register saved for {dateformat.format(d, 'j F Y')}")
    return redirect("attendance:register", arm_id=arm.pk, day=d.isoformat())


def _page(
    request: Any,
    ctx: dict[str, Any],
    term: Term,
    *,
    editable: bool,
    locked: bool,
    status: int = 200,
) -> HttpResponse:
    rows = reg.rows_for(ctx["arm"], ctx["day"], term=term)
    posted: dict[int, str] = {}
    if ctx.get("errors"):
        try:
            posted = _statuses(request.POST)
        except ValidationError:
            posted = {}
    counts = dict.fromkeys(reg.STATUSES, 0)
    for row in rows:
        if row.elsewhere is not None:
            continue
        row.status = posted.get(row.student.pk, row.status)
        if row.status is None and editable:
            row.status = "present"  # a new register: the teacher taps only the exceptions
        if row.status in counts:
            counts[row.status] += 1
    ctx.update(rows=rows, editable=editable, locked=locked, counts=counts, statuses=reg.STATUSES)
    return render(request, "attendance/register.html", ctx, status=status)
