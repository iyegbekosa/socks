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
from apps.attendance.days import RegisterDateError, check_register_date, school_days
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
    """Today's register, or the date chosen in the register page's date picker (``?date=``).

    Today being a weekend or holiday sends the user to the term's most recent school day."""
    arm = get_arm(arm_id)
    term, _ = _access(request, arm)
    now = timezone.localdate()
    chosen = reg.parse_iso_date(request.GET.get("date", ""))
    if chosen is not None:
        return redirect("attendance:register", arm_id=arm.pk, day=chosen.isoformat())
    if term is None:
        return _no_day(request, arm, "There is no current term.")
    try:
        check_register_date(now, term=term)
    except RegisterDateError:
        days = school_days(term, until=now)
        if not days:
            return _no_day(request, arm, "There has been no school day yet this term.")
        last = days[-1]
        messages.info(
            request, f"Today is not a school day — showing {dateformat.format(last, 'j F Y')}."
        )
        return redirect("attendance:register", arm_id=arm.pk, day=last.isoformat())
    return redirect("attendance:register", arm_id=arm.pk, day=now.isoformat())


def _no_day(request: Any, arm: ClassArm, message: str) -> HttpResponse:
    ctx = {"arm": arm, "day": timezone.localdate(), "no_day": message}
    return render(request, "attendance/register.html", ctx)


def _nav(ctx: dict[str, Any], term: Term | None) -> None:
    """The date picker's range and the previous/next school-day links (none past today)."""
    if term is None:
        return
    d = ctx["day"]
    last = min(timezone.localdate(), term.end_date)
    days = school_days(term, until=last)
    ctx["nav"] = {
        "min": term.start_date,
        "max": last,
        "prev": next((x for x in reversed(days) if x < d), None),
        "next": next((x for x in days if x > d), None),
    }


@never_cache
@login_required
def register_page(request, arm_id, day):
    arm = get_arm(arm_id)
    d = parse_day(day)
    term, level = _access(request, arm)
    ctx: dict[str, Any] = {"arm": arm, "day": d}
    if request.method == "POST":
        return _save(request, ctx, term, level)
    _nav(ctx, term)
    try:
        term = check_register_date(d, term=term)
    except RegisterDateError as exc:
        return _date_error(request, ctx, exc, term)
    locked = reg.is_locked(arm, term)
    editable = not locked and reg.editable_by(level, d, term)
    return _page(request, ctx, term, editable=editable, locked=locked)


def _date_error(
    request: Any, ctx: dict[str, Any], exc: RegisterDateError, term: Term | None
) -> HttpResponse:
    ctx["date_error"] = exc.message
    if "nav" not in ctx:
        _nav(ctx, term)
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
        return _date_error(request, ctx, exc, term)
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
    # Default-present makes an untaken register look like a saved all-present one: say so.
    ctx["taken"] = any(r.status is not None for r in rows if r.elsewhere is None)
    if "nav" not in ctx:
        _nav(ctx, term)
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
