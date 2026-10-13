import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from apps.academics.models import ClassLevel, Term
from apps.academics.services import current_session, current_term
from apps.accounts.roles import SCHOOL_ADMIN, require_roles
from apps.core.parsing import parse_id
from apps.fees import services
from apps.fees.forms import (
    CopyStructuresForm,
    DiscountForm,
    FeeItemForm,
    FeeSettingsForm,
    StructureForm,
    StudentDiscountForm,
)
from apps.fees.models import (
    Discount,
    FeeItem,
    FeeSettings,
    FeeStructure,
    StudentDiscount,
)
from apps.fees.scope import VIEW_ROLES, can_manage_fees
from apps.students.models import Student

logger = logging.getLogger(__name__)

CONFLICT_MESSAGE = "Someone else changed this at the same time. Please try again."


def _require_manage(request) -> None:
    if not can_manage_fees(request.user):
        raise PermissionDenied


def _save_or_conflict(request, save):
    """Run ``save``; a lost uniqueness race becomes a friendly message. Returns result or None."""
    try:
        return save()
    except IntegrityError as exc:
        logger.warning("fees conflict: %s", type(exc).__name__)
        messages.error(request, CONFLICT_MESSAGE)
        return None


@never_cache
@require_roles(*VIEW_ROLES)
def settings_page(request):
    can_edit = can_manage_fees(request.user)
    can_edit_threshold = can_edit and request.user.has_role(SCHOOL_ADMIN)
    conf = FeeSettings.load() if can_edit else _settings_or_defaults()
    if request.method == "POST":
        _require_manage(request)
        form = FeeSettingsForm(request.POST, conf=conf, can_edit_threshold=can_edit_threshold)
        if form.is_valid():
            services.save_settings(form.values(), by=request.user, request=request)
            messages.success(request, "Fee settings saved.")
            return redirect("fees:settings")
    else:
        form = FeeSettingsForm(conf=conf, can_edit_threshold=can_edit_threshold)
    return render(
        request,
        "fees/settings.html",
        {"form": form, "can_edit": can_edit, "can_edit_threshold": can_edit_threshold},
    )


def _settings_or_defaults() -> FeeSettings:
    return FeeSettings.objects.filter(pk=1).first() or FeeSettings(pk=1)


@never_cache
@require_roles(*VIEW_ROLES)
def items(request):
    can_edit = can_manage_fees(request.user)
    if request.method == "POST":
        _require_manage(request)
        form = FeeItemForm(request.POST)
        if form.is_valid():
            saved = _save_or_conflict(
                request, lambda: services.save_item(form.build(), by=request.user, request=request)
            )
            if saved is not None:
                messages.success(request, f"Added {saved.name}.")
                return redirect("fees:items")
    else:
        form = FeeItemForm()
    return render(
        request,
        "fees/items.html",
        {"items": FeeItem.objects.all(), "form": form, "can_edit": can_edit},
    )


@never_cache
@require_roles(*VIEW_ROLES)
def item_edit(request, item_id):
    item = get_object_or_404(FeeItem, pk=item_id)
    can_edit = can_manage_fees(request.user)
    if request.method == "POST":
        _require_manage(request)
        form = FeeItemForm(request.POST, instance=item)
        if form.is_valid():
            saved = _save_or_conflict(
                request, lambda: services.save_item(form.build(), by=request.user, request=request)
            )
            if saved is not None:
                messages.success(request, f"Saved {saved.name}.")
                return redirect("fees:items")
    else:
        form = FeeItemForm(instance=item)
    return render(
        request, "fees/item_edit.html", {"item": item, "form": form, "can_edit": can_edit}
    )


def _pick_term(request) -> Term | None:
    raw = request.GET.get("term")
    if raw is not None:
        term_id = parse_id(raw)
        if term_id is None:
            return None
        return get_object_or_404(Term.objects.select_related("session"), pk=term_id)
    return current_term() or Term.objects.select_related("session").last()


@never_cache
@require_roles(*VIEW_ROLES)
def structures(request):
    can_edit = can_manage_fees(request.user)
    copy_form = CopyStructuresForm()
    if request.method == "POST":
        _require_manage(request)
        copy_form = CopyStructuresForm(request.POST)
        if copy_form.is_valid():
            try:
                count = services.copy_structures(
                    copy_form.cleaned_data["from_term"],
                    copy_form.cleaned_data["to_term"],
                    request.user,
                    request=request,
                )
            except ValidationError as exc:
                copy_form.add_error(None, exc.messages[0])
            else:
                messages.success(request, f"Copied {count} fee structure(s).")
                return redirect("fees:structures")
    term = _pick_term(request)
    if request.GET.get("term") is not None and term is None:
        raise Http404
    rows = []
    if term is not None:
        existing = {
            s.level_id: s for s in FeeStructure.objects.filter(term=term).prefetch_related("lines")
        }
        for level in ClassLevel.objects.all():
            structure = existing.get(level.pk)
            rows.append(
                {
                    "level": level,
                    "structure": structure,
                    "lines": structure.lines.all() if structure else [],
                }
            )
    return render(
        request,
        "fees/structures.html",
        {
            "term": term,
            "terms": Term.objects.select_related("session"),
            "rows": rows,
            "copy_form": copy_form,
            "can_edit": can_edit,
        },
    )


@never_cache
@require_roles(*VIEW_ROLES)
def structure_edit(request, level_pk, term_pk):
    level = get_object_or_404(ClassLevel, pk=level_pk)
    term = get_object_or_404(Term.objects.select_related("session"), pk=term_pk)
    can_edit = can_manage_fees(request.user)
    item_list = list(FeeItem.objects.all())
    structure = FeeStructure.objects.filter(level=level, term=term).first()
    if request.method == "POST":
        _require_manage(request)
        form = StructureForm(item_list, request.POST)
        if form.is_valid():
            saved = _save_or_conflict(
                request,
                lambda: services.save_structure(
                    level, term, form.lines(), by=request.user, request=request
                ),
            )
            if saved is not None:
                messages.success(request, f"Saved fees for {level} {term}.")
                return redirect(f"{reverse('fees:structures')}?term={term.pk}")
    else:
        initial = {}
        if structure is not None:
            for line in structure.lines.all():
                initial[f"amount_{line.item_id}"] = f"{line.amount_kobo / 100:.2f}"
                initial[f"optional_{line.item_id}"] = line.is_optional
        form = StructureForm(item_list, initial=initial)
    return render(
        request,
        "fees/structure_edit.html",
        {
            "level": level,
            "term": term,
            "form": form,
            "can_edit": can_edit,
            "has_items": bool(item_list),
        },
    )


@never_cache
@require_roles(*VIEW_ROLES)
def discounts(request):
    can_edit = can_manage_fees(request.user)
    if request.method == "POST":
        _require_manage(request)
        form = DiscountForm(request.POST)
        if form.is_valid():
            saved = _save_or_conflict(
                request,
                lambda: services.save_discount(form.build(), by=request.user, request=request),
            )
            if saved is not None:
                messages.success(request, f"Added {saved.name}.")
                return redirect("fees:discounts")
    else:
        form = DiscountForm()
    return render(
        request,
        "fees/discounts.html",
        {"discounts": Discount.objects.all(), "form": form, "can_edit": can_edit},
    )


@never_cache
@require_roles(*VIEW_ROLES)
def discount_edit(request, discount_id):
    discount = get_object_or_404(Discount, pk=discount_id)
    can_edit = can_manage_fees(request.user)
    if request.method == "POST":
        _require_manage(request)
        form = DiscountForm(request.POST, instance=discount)
        if form.is_valid():
            saved = _save_or_conflict(
                request,
                lambda: services.save_discount(form.build(), by=request.user, request=request),
            )
            if saved is not None:
                messages.success(request, f"Saved {saved.name}.")
                return redirect("fees:discounts")
    else:
        form = DiscountForm(instance=discount)
    return render(
        request,
        "fees/discount_edit.html",
        {"discount": discount, "form": form, "can_edit": can_edit},
    )


@never_cache
@require_roles(*VIEW_ROLES)
def student_discounts(request, student_pk):
    student = get_object_or_404(Student.objects.select_related("user"), pk=student_pk)
    can_edit = can_manage_fees(request.user)
    rows = StudentDiscount.objects.filter(student=student).select_related("discount", "session")
    if request.method == "POST":
        _require_manage(request)
        if "remove" in request.POST:
            row_id = parse_id(request.POST.get("remove", ""))
            if row_id is None:
                raise Http404
            row = get_object_or_404(rows, pk=row_id)
            services.remove_discount(row, by=request.user, request=request)
            messages.success(request, "Discount removed.")
            return redirect("fees:student_discounts", student_pk=student.pk)
        form = StudentDiscountForm(request.POST, student=student)
        if form.is_valid():
            d = form.cleaned_data
            saved = _save_or_conflict(
                request,
                lambda: services.assign_discount(
                    student, d["discount"], d["session"], by=request.user, request=request
                ),
            )
            if saved is not None:
                messages.success(request, "Discount assigned.")
                return redirect("fees:student_discounts", student_pk=student.pk)
    else:
        form = StudentDiscountForm(student=student, initial={"session": current_session()})
    return render(
        request,
        "fees/student_discounts.html",
        {"student": student, "rows": rows, "form": form, "can_edit": can_edit},
    )
