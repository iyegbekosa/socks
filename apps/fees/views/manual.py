"""Manual payments (UC-13): record, the pending list, approve/reject and the teller photo."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.roles import require_roles
from apps.core.parsing import parse_id
from apps.fees import manual
from apps.fees.forms import ManualPaymentForm
from apps.fees.models import Invoice, ManualPayment
from apps.fees.scope import MANAGE_ROLES, VIEW_ROLES, can_manage_fees
from apps.fees.views.setup import _require_manage, _settings_or_defaults
from apps.students.models import Student
from apps.tenancy.limits import PlanLimitExceeded

FORM_FIELDS = {"method", "amount", "paid_on", "reference_no", "photo"}


def _get_or_404(model, raw):
    pk = parse_id(str(raw))
    if pk is None:
        raise Http404
    return get_object_or_404(model, pk=pk)


def _open_invoices(student: Student) -> list[Invoice]:
    return list(
        Invoice.objects.filter(student=student, total_kobo__gt=0)
        .exclude(status__in=(Invoice.CARRIED, Invoice.PAID))
        .select_related("term__session")
    )


def _add_errors(form: ManualPaymentForm, exc: ValidationError) -> None:
    if not hasattr(exc, "error_dict"):
        form.add_error(None, exc.messages)
        return
    for key, msgs in exc.message_dict.items():
        form.add_error(key if key in FORM_FIELDS else None, msgs)


@never_cache
@require_roles(*MANAGE_ROLES)
def manual_new(request, student_pk):
    _require_manage(request)
    student = _get_or_404(Student, student_pk)
    invoices = _open_invoices(student)
    preselect = None
    raw = request.GET.get("invoice")
    if raw not in (None, ""):
        preselect = parse_id(raw)
        if preselect is None or not Invoice.objects.filter(pk=preselect, student=student).exists():
            raise Http404

    if request.method == "POST":
        form = ManualPaymentForm(request.POST, request.FILES, invoices=invoices)
        if form.is_valid():
            data = form.cleaned_data
            try:
                mp = manual.record(
                    student=student,
                    method=data["method"],
                    amount_kobo=data["amount"],
                    plan=form.plan(),
                    paid_on=data["paid_on"],
                    reference_no=data["reference_no"],
                    photo=data["photo"] or None,
                    by=request.user,
                    request=request,
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            except PlanLimitExceeded as exc:
                form.add_error("photo", exc.message)
            else:
                if mp.status == ManualPayment.PENDING:
                    messages.success(
                        request,
                        "Payment recorded. It is waiting for approval by another bursar or "
                        "school admin.",
                    )
                    return redirect("fees:manual_pending")
                receipt = mp.payment.receipt
                messages.success(request, f"Payment recorded. Receipt {receipt.number}.")
                return redirect("fees:invoice", invoice_id=mp.plan[0]["invoice_id"])
    else:
        form = ManualPaymentForm(invoices=invoices, preselect=preselect)
    return render(
        request,
        "fees/manual_new.html",
        {
            "student": student,
            "form": form,
            "invoices": invoices,
            "threshold": _settings_or_defaults().manual_approval_threshold_kobo,
        },
    )


def _pending_page(request, *, error: str = "", status: int = 200, reject_errors=None):
    pending = ManualPayment.objects.filter(status=ManualPayment.PENDING).select_related(
        "student", "recorded_by"
    )
    rows = [
        {
            "mp": mp,
            "invoices": [line["invoice_id"] for line in mp.plan],
            "own": mp.recorded_by_id == request.user.pk,
        }
        for mp in pending
    ]
    return render(
        request,
        "fees/manual_pending.html",
        {
            "rows": rows,
            "can_act": can_manage_fees(request.user),
            "error": error,
            "reject_errors": reject_errors or {},
        },
        status=status,
    )


@never_cache
@require_roles(*VIEW_ROLES)
def manual_pending(request):
    return _pending_page(request)


@never_cache
@require_roles(*MANAGE_ROLES)
@require_POST
def manual_decide(request, pk):
    _require_manage(request)
    mp = _get_or_404(ManualPayment, pk)
    action = request.POST.get("action")
    if action not in ("approve", "reject"):
        return HttpResponseBadRequest("Unknown action.")
    try:
        if action == "approve":
            payment = manual.approve(mp, by=request.user, request=request)
            msg = f"Payment approved. Receipt {payment.receipt.number}."
            if payment.flagged:
                msg += " Part of it was more than the balance and is flagged for review."
            messages.success(request, msg)
        else:
            # The service checks permission and state first, then the reason.
            reason = request.POST.get("reason", "")
            manual.reject(mp, reason=reason, by=request.user, request=request)
            messages.success(request, "Payment rejected.")
    except manual.ManualStateError as exc:
        return _pending_page(request, error=exc.messages[0], status=409)
    except ValidationError as exc:
        return _pending_page(request, status=400, reject_errors={mp.pk: exc.messages})
    return redirect("fees:manual_pending")


@never_cache
@require_roles(*VIEW_ROLES)
def teller_photo(request, pk):
    mp = _get_or_404(ManualPayment, pk)
    if not mp.photo_name:
        raise Http404
    response = HttpResponseRedirect(default_storage.url(mp.photo_name))
    response["Cache-Control"] = "no-store"
    return response
