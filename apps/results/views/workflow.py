from collections.abc import Callable
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.academics.models import ClassArm, FormTeacher, SubjectOffering, Term
from apps.academics.services import current_term
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, TEACHER, require_roles
from apps.core.parsing import parse_id
from apps.results.models import ClassResult, ReportCard, ResultSummary, ScoreSheet
from apps.results.scope import can_open_comments, required_offerings
from apps.results.views.grid import NO_TERM, WRONG_SESSION, _offering_or_403
from apps.results.workflow import (
    ResultsStateError,
    approve_class,
    can_manage_class,
    is_form_teacher,
    needs_compute,
    publish_class,
    recall_sheet,
    regenerate_report_cards,
    retry_compute,
    review_class,
    submit_sheet,
    unlock_class,
)

BAD_SHEETS = "Choose score sheets from this class only."


def _arm(arm_pk) -> ClassArm:
    pk = parse_id(str(arm_pk))
    return get_object_or_404(ClassArm.objects.select_related("level"), pk=pk if pk else 0)


def _can_view(user: Any, arm: ClassArm, term: Term | None) -> bool:
    if user.has_role(PRINCIPAL, SCHOOL_ADMIN, PROPRIETOR):
        return True
    if term is None:
        return FormTeacher.objects.filter(arm=arm, teacher=user).exists()
    return is_form_teacher(user, arm, term)


def _context(request, arm: ClassArm, term: Term) -> dict[str, Any]:
    user = request.user
    klass = ClassResult.objects.filter(arm=arm, term=term).first()
    status = klass.status if klass else ClassResult.Status.DRAFT
    offerings = (
        SubjectOffering.objects.filter(arm=arm, session_id=term.session_id)
        .select_related("subject", "teacher")
        .order_by("subject__name", "pk")
    )
    required = set(required_offerings(arm, term).values_list("pk", flat=True))
    sheets = {s.offering_id: s for s in ScoreSheet.objects.filter(term=term, offering__arm=arm)}
    rows = []
    for o in offerings:
        sheet = sheets.get(o.pk)
        rows.append(
            {
                "offering": o,
                "teacher": o.teacher.get_full_name() if o.teacher else "",
                "required": o.pk in required,
                "sheet": sheet,
                "status": sheet.status if sheet else ScoreSheet.Status.DRAFT,
            }
        )
    submitted = [r for r in rows if r["status"] == ScoreSheet.Status.SUBMITTED]
    is_admin = user.has_role(SCHOOL_ADMIN)
    published = status == ClassResult.Status.PUBLISHED
    return {
        "arm": arm,
        "term": term,
        "klass": klass,
        "status": status,
        "status_label": ClassResult.Status(status).label,
        "rows": rows,
        "submitted": submitted,
        "is_ft": is_form_teacher(user, arm, term),
        "can_comments": can_open_comments(user, arm, term),
        "show_broadsheet": status in (ClassResult.Status.APPROVED, ClassResult.Status.PUBLISHED),
        "can_review": status == ClassResult.Status.DRAFT and is_form_teacher(user, arm, term),
        "can_approve": status == ClassResult.Status.REVIEWED and user.has_role(PRINCIPAL),
        "can_publish": status == ClassResult.Status.APPROVED
        and user.has_role(PRINCIPAL, SCHOOL_ADMIN),
        "can_unlock": is_admin and (status != ClassResult.Status.DRAFT or bool(submitted)),
        "can_retry_compute": can_manage_class(user) and needs_compute(klass),
        "can_regenerate": published and can_manage_class(user),
        "cards": _cards(arm, term) if published else [],
    }


def _cards(arm: ClassArm, term: Term) -> list[dict[str, Any]]:
    """Each ranked student of a published class with their report card, if generated yet."""
    have = set(ReportCard.objects.filter(arm=arm, term=term).values_list("student_id", flat=True))
    return [
        {"student": s.student, "ready": s.student_id in have}
        for s in ResultSummary.objects.filter(arm=arm, term=term)
        .select_related("student")
        .order_by("student__last_name", "student__first_name", "student_id")
    ]


def _page(request, arm, term, *, errors=None, reason="", code=200):
    ctx: dict[str, Any] = {"arm": arm, "term": term, "errors": errors or [], "reason": reason}
    if term is not None:
        ctx.update(_context(request, arm, term))
    return render(request, "results/class_status.html", ctx, status=code)


@never_cache
@require_GET
@require_roles(TEACHER, PRINCIPAL, SCHOOL_ADMIN, PROPRIETOR)
def class_status(request, arm_pk):
    arm = _arm(arm_pk)
    term = current_term()
    if not _can_view(request.user, arm, term):
        raise PermissionDenied
    return _page(request, arm, term)


def _act(request, arm_pk, action: Callable[[ClassArm, Term], Any], done: str):
    arm = _arm(arm_pk)
    term = current_term()
    if term is None:
        return _page(request, arm, None, errors=[NO_TERM], code=409)
    try:
        action(arm, term)
    except ResultsStateError as exc:
        return _page(request, arm, term, errors=exc.messages, code=409)
    except ValidationError as exc:
        return _page(request, arm, term, errors=exc.messages, code=400)
    messages.success(request, done)
    return redirect("results:class_status", arm_pk=arm.pk)


@never_cache
@require_POST
@require_roles(TEACHER)
def class_review(request, arm_pk):
    return _act(
        request,
        arm_pk,
        lambda arm, term: review_class(arm, term, by=request.user, request=request),
        "Class results reviewed",
    )


@never_cache
@require_POST
@require_roles(PRINCIPAL)
def class_approve(request, arm_pk):
    return _act(
        request,
        arm_pk,
        lambda arm, term: approve_class(arm, term, by=request.user, request=request),
        "Class results approved. They are being computed.",
    )


@never_cache
@require_POST
@require_roles(PRINCIPAL, SCHOOL_ADMIN)
def class_publish(request, arm_pk):
    return _act(
        request,
        arm_pk,
        lambda arm, term: publish_class(arm, term, by=request.user, request=request),
        "Class results published",
    )


@never_cache
@require_POST
@require_roles(PRINCIPAL, SCHOOL_ADMIN)
def class_retry_compute(request, arm_pk):
    return _act(
        request,
        arm_pk,
        lambda arm, term: retry_compute(arm, term, by=request.user, request=request),
        "Computing the class results again.",
    )


@never_cache
@require_POST
@require_roles(PRINCIPAL, SCHOOL_ADMIN)
def class_regenerate_cards(request, arm_pk):
    return _act(
        request,
        arm_pk,
        lambda arm, term: regenerate_report_cards(arm, term, by=request.user, request=request),
        "Report cards are being generated again.",
    )


@never_cache
@require_POST
@require_roles(SCHOOL_ADMIN)
def class_unlock(request, arm_pk):
    arm = _arm(arm_pk)
    term = current_term()
    reason = request.POST.get("reason", "")
    if term is None:
        return _page(request, arm, None, errors=[NO_TERM], code=409)
    ids = [parse_id(raw) for raw in request.POST.getlist("sheets")]
    if any(i is None for i in ids):
        return _page(request, arm, term, errors=[BAD_SHEETS], reason=reason, code=400)
    sheets = list(ScoreSheet.objects.filter(pk__in=ids))
    if len(sheets) != len(set(ids)):
        return _page(request, arm, term, errors=[BAD_SHEETS], reason=reason, code=400)
    try:
        unlock_class(arm, term, sheets=sheets, reason=reason, by=request.user, request=request)
    except ResultsStateError as exc:
        return _page(request, arm, term, errors=exc.messages, reason=reason, code=409)
    except ValidationError as exc:
        return _page(request, arm, term, errors=exc.messages, reason=reason, code=400)
    messages.success(request, "Class results unlocked")
    return redirect("results:class_status", arm_pk=arm.pk)


def _sheet_action(request, offering_pk, action, done: str):
    offering = _offering_or_403(request, offering_pk)
    term = current_term()
    if term is None:
        messages.error(request, NO_TERM)
    elif term.session_id != offering.session_id:
        messages.error(request, WRONG_SESSION)
    else:
        try:
            action(offering, term, by=request.user, request=request)
        except ValidationError as exc:
            for m in exc.messages:
                messages.error(request, m)
        else:
            messages.success(request, done)
    return redirect("results:grid", offering_pk=offering.pk)


@never_cache
@require_POST
@require_roles(TEACHER)
def sheet_submit(request, offering_pk):
    return _sheet_action(request, offering_pk, submit_sheet, "Score sheet submitted")


@never_cache
@require_POST
@require_roles(TEACHER)
def sheet_recall(request, offering_pk):
    return _sheet_action(request, offering_pk, recall_sheet, "Score sheet recalled")
