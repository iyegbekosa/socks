"""Debtors list with CSV export, and the fees dashboard (UC-14)."""

import csv
import io
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import F, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache

from apps.academics.models import ClassArm, ClassLevel, Term
from apps.accounts.roles import require_roles
from apps.core.money import kobo_from_naira
from apps.fees import debtors as debtors_service
from apps.fees.models import Allocation, Invoice, ManualPayment, Payment, PaymentAttempt
from apps.fees.scope import VIEW_ROLES, can_manage_fees
from apps.fees.views.invoices import _filter_obj
from apps.fees.views.setup import _pick_term
from apps.results.csvsheet import safe_cell

LAGOS = ZoneInfo("Africa/Lagos")
CSV_HEADER = ["Admission no.", "Name", "Class", "Total", "Paid", "Balance", "Last payment"]
NO_CLASS = "—"


def _term_or_404(request) -> Term | None:
    term = _pick_term(request)
    if request.GET.get("term") is not None and term is None:
        raise Http404
    return term


def _plain_naira(kobo: int) -> str:
    """``50000.00``: no ₦ sign or separators, so spreadsheets can sort and sum it."""
    sign = "-" if kobo < 0 else ""
    whole, fraction = divmod(abs(kobo), 100)
    return f"{sign}{whole}.{fraction:02d}"


def _lagos_text(when: datetime | None) -> str:
    return "" if when is None else when.astimezone(LAGOS).strftime("%Y-%m-%d %H:%M")


def _csv_response(rows: list[debtors_service.DebtorRow], term: Term) -> HttpResponse:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            [
                safe_cell(row.student.admission_number or ""),
                safe_cell(row.student.full_name),
                safe_cell(row.arm.display_name if row.arm is not None else NO_CLASS),
                _plain_naira(row.total_kobo),
                _plain_naira(row.paid_kobo),
                _plain_naira(row.balance_kobo),
                _lagos_text(row.last_payment_at),
            ]
        )
    response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="debtors-{term.pk}.csv"'
    response["Cache-Control"] = "no-store"
    return response


@require_roles(*VIEW_ROLES)
def debtors(request):
    """The page is ``never_cache``; the CSV is exactly ``Cache-Control: no-store``."""
    term = _term_or_404(request)
    level = _filter_obj(request, "level", ClassLevel)
    arm = _filter_obj(request, "arm", ClassArm)
    raw_min = (request.GET.get("min") or "").strip().replace(",", "")
    min_error = ""
    min_kobo = 1
    if raw_min:
        try:
            min_kobo = max(1, kobo_from_naira(raw_min))
        except (TypeError, ValueError):
            min_error = "Enter a valid amount in naira, for example 20000."
    rows = (
        debtors_service.debtors(term, level=level, arm=arm, min_balance_kobo=min_kobo)
        if term is not None
        else []
    )
    if term is not None and request.GET.get("export") == "csv" and not min_error:
        return _csv_response(rows, term)
    response = render(
        request,
        "fees/debtors.html",
        {
            "term": term,
            "terms": Term.objects.select_related("session"),
            "levels": ClassLevel.objects.all(),
            "arms": ClassArm.objects.select_related("level"),
            "level": level,
            "arm": arm,
            "raw_min": raw_min,
            "min_error": min_error,
            "rows": rows,
            "total_balance_kobo": sum(r.balance_kobo for r in rows),
            "no_class": NO_CLASS,
        },
    )
    add_never_cache_headers(response)
    return response


def _today_bounds() -> tuple[datetime, datetime]:
    """Start and end of today's Africa/Lagos calendar date."""
    today = timezone.now().astimezone(LAGOS).date()
    start = datetime.combine(today, time.min, tzinfo=LAGOS)
    return start, start + timedelta(days=1)


@never_cache
@require_roles(*VIEW_ROLES)
def dashboard(request):
    term = _term_or_404(request)
    invoiced = collected = outstanding = 0
    if term is not None:
        invoiced = Invoice.objects.filter(term=term).aggregate(t=Sum("total_kobo"))["t"] or 0
        collected = (
            Allocation.objects.filter(invoice__term=term).aggregate(t=Sum("amount_kobo"))["t"] or 0
        )
        outstanding = (
            Invoice.objects.filter(term=term)
            .exclude(status=Invoice.CARRIED)
            .aggregate(t=Sum(F("total_kobo") - F("paid_kobo")))["t"]
            or 0
        )
    start, end = _today_bounds()
    today = (
        Payment.objects.filter(paid_at__gte=start, paid_at__lt=end).aggregate(t=Sum("amount_kobo"))[
            "t"
        ]
        or 0
    )
    pending = ManualPayment.objects.filter(status=ManualPayment.PENDING).count()
    flagged = (
        Payment.objects.filter(flagged=True).count()
        + PaymentAttempt.objects.filter(status=PaymentAttempt.FLAGGED).count()
    )
    return render(
        request,
        "fees/dashboard.html",
        {
            "term": term,
            "terms": Term.objects.select_related("session"),
            "invoiced_kobo": int(invoiced),
            "collected_kobo": int(collected),
            "outstanding_kobo": int(outstanding),
            "today_kobo": int(today),
            "pending_count": pending,
            "flagged_count": flagged,
            "can_edit": can_manage_fees(request.user),
        },
    )
