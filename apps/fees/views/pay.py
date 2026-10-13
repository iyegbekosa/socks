"""Student online payment (§4.5): choose invoices, go to the gateway, come back to a status page.

The return page only reads the attempt's status (and its receipt once confirmed); confirmation
is the webhook's job (``apps.fees.webhooks`` → ``confirm.process_attempt``).
"""

from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from apps.core.parsing import parse_id
from apps.fees import checkout
from apps.fees.forms import naira_input, parse_naira
from apps.fees.gateways import GatewayUnavailable
from apps.fees.ledger import PlannedLine
from apps.fees.models import FeeSettings, PaymentAttempt, Receipt
from apps.students.models import Student
from apps.tenancy.limits import _current_school


def _student(request) -> Student:
    if getattr(request.user, "kind", None) != "student":
        raise PermissionDenied
    student = Student.objects.filter(user=request.user).first()
    if student is None:
        raise Http404
    return student


MAX_PRESELECT = 50


def _preselected(student: Student, get: Any) -> set[int]:
    """``?with=<student id>`` (repeatable): students whose invoices start ticked, plus the
    student's own. Only preselects: rows still come from ``payable_invoices`` and ownership
    is enforced again on POST. A malformed id is a 404."""
    raw_ids = get.getlist("with")[:MAX_PRESELECT]
    ids = set()
    for raw in raw_ids:
        pk = parse_id(raw)
        if pk is None:
            raise Http404
        ids.add(pk)
    return {student.pk, *ids} if ids else set()


def _lines(post: Any) -> list[PlannedLine]:
    """``invoice`` ids (a bad id is a 404) with their ``amount_<id>`` naira values."""
    lines = []
    for raw in post.getlist("invoice"):
        pk = parse_id(raw)
        if pk is None:
            raise Http404
        try:
            kobo = parse_naira(post.get(f"amount_{pk}", ""))
        except ValidationError:
            raise ValidationError(checkout.MSG_AMOUNT) from None
        lines.append(PlannedLine(pk, kobo))
    return lines


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def pay(request):
    student = _student(request)
    school = _current_school()
    ready = school is not None and school.online_payments_ready
    conf = FeeSettings.objects.filter(pk=1).first() or FeeSettings(pk=1)
    error = ""
    unavailable = ""
    selected: dict[int, str] = {}
    preselect = _preselected(student, request.GET) if request.method == "GET" else set()

    if request.method == "POST" and ready:
        try:
            lines = _lines(request.POST)
            selected = {ln.invoice_id: naira_input(ln.amount_kobo) for ln in lines}
            attempt = checkout.start(
                student,
                lines,
                callback_url=request.build_absolute_uri(reverse("fees:pay_return")),
                request=request,
            )
        except checkout.OnlinePaymentsNotReady:
            ready = False
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except GatewayUnavailable:
            unavailable = checkout.MSG_GATEWAY_DOWN
        else:
            return redirect(attempt.authorization_url)

    rows = []
    if ready:
        for inv in checkout.payable_invoices(student):
            rows.append(
                {
                    "invoice": inv,
                    "own": inv.student_id == student.pk,
                    "first_name": inv.student.first_name,
                    "checked": inv.pk in selected or inv.student_id in preselect,
                    "amount": selected.get(inv.pk) or naira_input(inv.balance_kobo),
                    "balance_input": naira_input(inv.balance_kobo),
                }
            )
    return render(
        request,
        "fees/pay.html",
        {
            "ready": ready,
            "not_ready_message": checkout.MSG_NOT_READY,
            "rows": rows,
            "allow_part": conf.allow_part_payment,
            "minimum": conf.min_part_payment_kobo,
            "error": error,
            "unavailable": unavailable,
            "bank_transfer_note": conf.bank_transfer_note,
        },
    )


@never_cache
@login_required
@require_GET
def pay_return(request):
    student = _student(request)
    references = request.GET.getlist("reference")
    reference = references[0] if references else ""
    status = checkout.status_for(student, reference)
    receipt = None
    if status == PaymentAttempt.SUCCESS:
        receipt = Receipt.objects.filter(
            payment__attempt__reference=reference, payment__attempt__student=student
        ).first()
    return render(
        request,
        "fees/pay_return.html",
        {"reference": reference, "status": status, "receipt": receipt},
    )
