from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.forms import ArmForm, ArmRenameForm, SessionForm, SubjectForm
from apps.academics.models import AcademicSession, ClassArm, ClassLevel, Subject, Term
from apps.academics.services import delete_arm, delete_subject, set_current_term
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles

CONFLICT_MESSAGE = "Someone else changed this at the same time — please try again."


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL)
def sessions(request):
    rows = AcademicSession.objects.prefetch_related("terms")
    return render(
        request,
        "academics/sessions.html",
        {"sessions": rows, "can_edit": request.user.has_role(SCHOOL_ADMIN)},
    )


@never_cache
@require_roles(SCHOOL_ADMIN)
def session_create(request):
    if request.method == "POST":
        form = SessionForm(request.POST)
        if form.is_valid():
            try:
                session = form.save(by=request.user, request=request)
            except IntegrityError:
                session = None
                messages.error(request, CONFLICT_MESSAGE)
            if session is not None:
                messages.success(request, f"Session {session.name} created.")
                return redirect("academics:sessions")
    else:
        form = SessionForm()
    return render(request, "academics/session_form.html", {"form": form})


@require_POST
@require_roles(SCHOOL_ADMIN)
def set_current(request, term_id):
    term = get_object_or_404(Term, pk=term_id)
    try:
        set_current_term(term, by=request.user, request=request)
    except IntegrityError:
        messages.error(request, CONFLICT_MESSAGE)
    else:
        messages.success(request, f"{term} is now the current term.")
    return redirect("academics:sessions")


def _save(request, form, ok_message):
    """Run form.save, turning a lost race into a friendly message. Returns the object or None."""
    try:
        obj = form.save(by=request.user, request=request)
    except IntegrityError:
        messages.error(request, CONFLICT_MESSAGE)
        return None
    if obj is not None:
        messages.success(request, ok_message(obj))
    return obj


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL)
def structure(request):
    can_edit = request.user.has_role(SCHOOL_ADMIN)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        form = ArmForm(request.POST)
        if form.is_valid() and _save(request, form, lambda a: f"Added {a.display_name}."):
            return redirect("academics:structure")
    else:
        form = ArmForm()
    levels = ClassLevel.objects.prefetch_related("arms")
    return render(
        request,
        "academics/structure.html",
        {"levels": levels, "form": form, "can_edit": can_edit},
    )


@never_cache
@require_roles(SCHOOL_ADMIN)
def arm_rename(request, arm_id):
    arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=arm_id)
    if request.method == "POST":
        form = ArmRenameForm(request.POST, arm=arm)
        if form.is_valid() and _save(request, form, lambda a: f"Saved {a.display_name}."):
            return redirect("academics:structure")
    else:
        form = ArmRenameForm(arm=arm)
    return render(request, "academics/arm_form.html", {"form": form, "arm": arm})


@require_POST
@require_roles(SCHOOL_ADMIN)
def arm_delete(request, arm_id):
    arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=arm_id)
    label = arm.display_name
    try:
        delete_arm(arm, by=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except IntegrityError:
        messages.error(request, CONFLICT_MESSAGE)
    else:
        messages.success(request, f"Deleted {label}.")
    return redirect("academics:structure")


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL)
def subjects(request):
    can_edit = request.user.has_role(SCHOOL_ADMIN)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        form = SubjectForm(request.POST)
        if form.is_valid() and _save(request, form, lambda s: f"Added {s.name}."):
            return redirect("academics:subjects")
    else:
        form = SubjectForm()
    return render(
        request,
        "academics/subjects.html",
        {"subjects": Subject.objects.all(), "form": form, "can_edit": can_edit},
    )


@never_cache
@require_roles(SCHOOL_ADMIN)
def subject_edit(request, subject_id):
    subject = get_object_or_404(Subject, pk=subject_id)
    if request.method == "POST":
        form = SubjectForm(request.POST, subject=subject)
        if form.is_valid() and _save(request, form, lambda s: f"Saved {s.name}."):
            return redirect("academics:subjects")
    else:
        form = SubjectForm(subject=subject)
    return render(request, "academics/subject_form.html", {"form": form, "subject": subject})


@require_POST
@require_roles(SCHOOL_ADMIN)
def subject_delete(request, subject_id):
    subject = get_object_or_404(Subject, pk=subject_id)
    label = subject.name
    try:
        delete_subject(subject, by=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except IntegrityError:
        messages.error(request, CONFLICT_MESSAGE)
    else:
        messages.success(request, f"Deleted {label}.")
    return redirect("academics:subjects")
