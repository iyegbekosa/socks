import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from apps.academics.models import SECTION_CHOICES, SECTIONS, ClassLevel
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles
from apps.results import scope
from apps.results.forms import BandFormSet, ComponentFormSet
from apps.results.models import AssessmentComponent, GradeBand
from apps.results.services import save_components, save_scale

logger = logging.getLogger(__name__)

CONFLICT_MESSAGE = "Someone else changed this at the same time — please try again."


def _sections() -> list[str]:
    present = set(ClassLevel.objects.values_list("section", flat=True))
    return [s for s in SECTIONS if s in present] or list(SECTIONS)


def _component_formset(section, data=None):
    initial = [
        {"name": c.name, "max_score": c.max_score, "order": c.order}
        for c in AssessmentComponent.objects.filter(section=section)
    ]
    return ComponentFormSet(data, initial=None if data else initial, prefix=f"c-{section}")


def _band_formset(section, data=None):
    initial = [
        {"lower": b.lower, "upper": b.upper, "grade": b.grade, "remark": b.remark}
        for b in GradeBand.objects.filter(section=section)
    ]
    return BandFormSet(data, initial=None if data else initial, prefix=f"b-{section}")


@never_cache
@require_roles(SCHOOL_ADMIN, PRINCIPAL, PROPRIETOR)
def settings_page(request):
    can_edit = request.user.has_role(SCHOOL_ADMIN)
    sections = _sections()
    bound: dict[tuple[str, str], object] = {}
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        section = request.POST.get("section")
        kind = request.POST.get("kind")
        if section not in sections or kind not in ("components", "scale"):
            raise PermissionDenied
        make = _component_formset if kind == "components" else _band_formset
        formset = make(section, request.POST)
        bound[(section, kind)] = formset
        if formset.is_valid():
            rows = formset.kept()
            try:
                if kind == "components":
                    save_components(section, rows, by=request.user, request=request)
                else:
                    save_scale(section, rows, by=request.user, request=request)
            except ValidationError as exc:
                formset._non_form_errors = formset.error_class(exc.messages)
            except IntegrityError as exc:
                logger.warning("results conflict: %s", type(exc).__name__)
                messages.error(request, CONFLICT_MESSAGE)
            else:
                messages.success(request, "Saved.")
                return redirect("results:settings")
    labels = dict(SECTION_CHOICES)
    panels = []
    for section in sections:
        comps = _component_formset(section)
        total = sum(c.max_score for c in AssessmentComponent.objects.filter(section=section))
        panels.append(
            {
                "section": section,
                "label": labels[section],
                "components": bound.get((section, "components"), comps),
                "scale": bound.get((section, "scale"), _band_formset(section)),
                "total": total,
                "complete": total == 100,
                "locked": scope.has_scores(section),
            }
        )
    return render(request, "results/settings.html", {"panels": panels, "can_edit": can_edit})
