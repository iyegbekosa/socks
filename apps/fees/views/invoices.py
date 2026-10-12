import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from apps.academics.models import ClassArm, ClassLevel, Term
from apps.accounts.roles import require_roles
from apps.core.parsing import parse_id
from apps.fees import invoicing
from apps.fees.forms import AdjustmentForm, GenerateInvoicesForm, OptionalItemForm
from apps.fees.models import Allocation, FeeStructureLine, Invoice, InvoiceLine
from apps.fees.scope import VIEW_ROLES, can_manage_fees
from apps.fees.views.setup import _pick_term, _require_manage
from apps.students.models import Enrolment

logger = logging.getLogger(__name__)

CARRIED_MESSAGE = (
    "This invoice's balance was carried forward to a later invoice, so it can no longer be "
    "changed. Adjust the newest invoice instead."
)
PAGE_SIZE = 100


def _filter_obj(request, name, model, **extra):
    raw = request.GET.get(name)
    if raw in (None, ""):
        return None
    obj_id = parse_id(raw)
    if obj_id is None:
        raise Http404
    return get_object_or_404(model.objects.filter(**extra), pk=obj_id)


@never_cache
@require_roles(*VIEW_ROLES)
def invoices(request):
    can_edit = can_manage_fees(request.user)
    if request.method == "POST":
        _require_manage(request)
        gen_form = GenerateInvoicesForm(request.POST)
        if gen_form.is_valid():
            term = gen_form.cleaned_data["term"]
            levels = list(gen_form.cleaned_data["levels"]) or None
            result = invoicing.generate_invoices(
                term, levels=levels, by=request.user, request=request
            )
            messages.success(
                request,
                f"Created {result.created} invoice(s). "
                f"{result.skipped_existing} student(s) already had one. "
                f"{result.skipped_no_structure} student(s) are in a class level with no fee "
                "structure for this term.",
            )
            return redirect(f"{reverse('fees:invoices')}?term={term.pk}")
    else:
        gen_form = None

    term = _pick_term(request)
    if request.GET.get("term") is not None and term is None:
        raise Http404
    level = _filter_obj(request, "level", ClassLevel)
    arm = _filter_obj(request, "arm", ClassArm)
    if gen_form is None:
        gen_form = GenerateInvoicesForm(initial={"term": term})

    page = None
    arms_by_student: dict[int, ClassArm] = {}
    if term is not None:
        qs = Invoice.objects.filter(term=term).select_related("student__user")
        enrol_filter = {"student__enrolments__session_id": term.session_id}
        if level is not None:
            enrol_filter["student__enrolments__arm__level"] = level
        if arm is not None:
            enrol_filter["student__enrolments__arm"] = arm
        if level is not None or arm is not None:
            qs = qs.filter(**enrol_filter)
        qs = qs.order_by("student__last_name", "student__first_name", "pk")
        page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
        student_ids = [inv.student_id for inv in page.object_list]
        for enrolment in Enrolment.objects.filter(
            session_id=term.session_id, student_id__in=student_ids
        ).select_related("arm__level"):
            arms_by_student[enrolment.student_id] = enrolment.arm
    rows = [
        {"invoice": inv, "arm": arms_by_student.get(inv.student_id)}
        for inv in (page.object_list if page is not None else [])
    ]
    return render(
        request,
        "fees/invoices.html",
        {
            "term": term,
            "terms": Term.objects.select_related("session"),
            "levels": ClassLevel.objects.all(),
            "arms": ClassArm.objects.select_related("level"),
            "level": level,
            "arm": arm,
            "page": page,
            "rows": rows,
            "gen_form": gen_form,
            "can_edit": can_edit,
        },
    )


def _optional_lines(invoice: Invoice, level_id: int | None):
    if level_id is None:
        return FeeStructureLine.objects.none()
    taken = invoice.lines.filter(kind=InvoiceLine.OPTIONAL, item__isnull=False).values("item_id")
    return (
        FeeStructureLine.objects.filter(
            structure__term_id=invoice.term_id, structure__level_id=level_id, is_optional=True
        )
        .exclude(item_id__in=taken)
        .select_related("item")
    )


@never_cache
@require_roles(*VIEW_ROLES)
def invoice_detail(request, invoice_id):
    pk = parse_id(invoice_id)
    if pk is None:
        raise Http404
    invoice = get_object_or_404(
        Invoice.objects.select_related("student__user", "term__session"), pk=pk
    )
    can_edit = can_manage_fees(request.user)
    enrolment = (
        Enrolment.objects.filter(student_id=invoice.student_id, session_id=invoice.term.session_id)
        .select_related("arm__level")
        .first()
    )
    level_id = enrolment.arm.level_id if enrolment is not None else None
    optional_form = OptionalItemForm(lines=_optional_lines(invoice, level_id))
    adjustment_form = AdjustmentForm()
    status = 200
    error = ""

    if request.method == "POST":
        _require_manage(request)
        action = request.POST.get("action")
        if action not in ("optional", "adjustment"):
            return HttpResponseBadRequest("Unknown action.")
        if invoice.status == Invoice.CARRIED:
            error, status = CARRIED_MESSAGE, 409
        else:
            if action == "optional":
                optional_form = OptionalItemForm(
                    request.POST, lines=_optional_lines(invoice, level_id)
                )
                form = optional_form
            else:
                adjustment_form = AdjustmentForm(request.POST)
                form = adjustment_form
            if form.is_valid():
                if action == "optional":
                    line = form.cleaned_data["line"]
                    kwargs = {
                        "kind": InvoiceLine.OPTIONAL,
                        "description": line.item.name,
                        "item": line.item,
                        "amount_kobo": line.amount_kobo,
                        "reason": "",
                    }
                else:
                    kwargs = {
                        "kind": InvoiceLine.ADJUSTMENT,
                        "description": "Adjustment",
                        "amount_kobo": form.cleaned_data["amount"],
                        "reason": form.cleaned_data["reason"],
                    }
                try:
                    invoicing.add_line(invoice, by=request.user, request=request, **kwargs)
                except invoicing.InvoiceCarried:
                    error, status = CARRIED_MESSAGE, 409
                except ValidationError as exc:
                    form.add_error(None, exc.messages[0])
                else:
                    messages.success(request, "Invoice updated.")
                    return redirect("fees:invoice", invoice_id=invoice.pk)

    return render(
        request,
        "fees/invoice_detail.html",
        {
            "invoice": invoice,
            "student": invoice.student,
            "arm": enrolment.arm if enrolment is not None else None,
            "lines": invoice.lines.select_related("created_by").order_by("pk"),
            "allocations": Allocation.objects.filter(invoice=invoice)
            .select_related("payment__receipt")
            .order_by("payment__paid_at", "pk"),
            "optional_form": optional_form,
            "adjustment_form": adjustment_form,
            "can_edit": can_edit and invoice.status != Invoice.CARRIED,
            "can_manage": can_edit,
            "error": error,
        },
        status=status,
    )
