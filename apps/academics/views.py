import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.forms import (
    AdmissionSettingsForm,
    ArmForm,
    ArmRenameForm,
    SessionForm,
    SubjectForm,
)
from apps.academics.models import (
    AcademicSession,
    ClassArm,
    ClassLevel,
    FormTeacher,
    Subject,
    SubjectOffering,
    Term,
)
from apps.academics.services import (
    academic_settings_or_defaults,
    academic_write_lock,
    assign_form_teacher,
    assign_offering,
    current_session,
    current_term,
    delete_arm,
    delete_subject,
    preview_next_admission_number,
    remove_form_teacher,
    set_current_term,
)
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles

logger = logging.getLogger(__name__)

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
            except IntegrityError as exc:
                logger.warning("academics conflict: %s", type(exc).__name__)
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
    except IntegrityError as exc:
        logger.warning("academics conflict: %s", type(exc).__name__)
        messages.error(request, CONFLICT_MESSAGE)
    else:
        messages.success(request, f"{term} is now the current term.")
    return redirect("academics:sessions")


def _save(request, form, ok_message):
    """Run form.save, turning a lost race into a friendly message. Returns the object or None."""
    try:
        obj = form.save(by=request.user, request=request)
    except IntegrityError as exc:
        logger.warning("academics conflict: %s", type(exc).__name__)
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
    except IntegrityError as exc:
        logger.warning("academics conflict: %s", type(exc).__name__)
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
    except IntegrityError as exc:
        logger.warning("academics conflict: %s", type(exc).__name__)
        messages.error(request, CONFLICT_MESSAGE)
    else:
        messages.success(request, f"Deleted {label}.")
    return redirect("academics:subjects")


def _active_teachers():
    return (
        get_user_model()
        .objects.filter(is_active=True, kind="staff", groups__name="teacher")
        .distinct()
        .order_by("first_name", "last_name")
    )


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL)
def arm_detail(request, arm_id):
    arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=arm_id)
    can_edit = request.user.has_role(SCHOOL_ADMIN)
    if request.method == "POST" and not can_edit:
        raise PermissionDenied
    session = current_session()
    from apps.results.scope import can_open_comments  # results builds on academics

    ctx = {
        "arm": arm,
        "can_edit": can_edit,
        "session": session,
        "can_comments": can_open_comments(request.user, arm, current_term()),
    }
    if session is None:
        return render(request, "academics/arm_detail.html", ctx)

    teachers = list(_active_teachers())
    by_id = {t.pk: t for t in teachers}
    subjects_here = [s for s in Subject.objects.all() if arm.level.section in (s.sections or [])]

    def pick(raw: str | None, errors: list[str], label: str):
        """Return the chosen teacher; blank means none. Bad choices add an error."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            teacher = by_id.get(int(raw))
        except ValueError:
            teacher = None
        if teacher is None:
            errors.append(f"{label}: choose an active teacher from the list.")
        return teacher

    if request.method == "POST":
        try:
            with transaction.atomic():
                academic_write_lock()
                # Re-read under the lock so the diff below is against current data.
                existing = {
                    o.subject_id: o
                    for o in SubjectOffering.objects.filter(arm=arm, session=session)
                }
                current_ft = FormTeacher.objects.filter(arm=arm, session=session).first()
                errors: list[str] = []
                new_ft = pick(request.POST.get("form_teacher"), errors, "Form teacher")
                plans = []
                for subject in subjects_here:
                    teacher = pick(request.POST.get(f"teacher_{subject.pk}"), errors, subject.name)
                    compulsory = f"compulsory_{subject.pk}" in request.POST
                    old = existing.get(subject.pk)
                    if old is None and teacher is None:
                        continue
                    if (
                        old is not None
                        and old.teacher_id == (teacher.pk if teacher else None)
                        and old.is_compulsory == compulsory
                    ):
                        continue
                    plans.append((subject, teacher, compulsory))
                if errors:
                    raise ValidationError(errors)
                changed = 0
                if (new_ft.pk if new_ft else None) != (
                    current_ft.teacher_id if current_ft else None
                ):
                    if new_ft is None:
                        remove_form_teacher(
                            arm=arm, session=session, by=request.user, request=request
                        )
                    else:
                        assign_form_teacher(
                            arm=arm,
                            session=session,
                            teacher=new_ft,
                            by=request.user,
                            request=request,
                        )
                    changed += 1
                for subject, teacher, compulsory in plans:
                    assign_offering(
                        subject=subject,
                        arm=arm,
                        session=session,
                        teacher=teacher,
                        is_compulsory=compulsory,
                        by=request.user,
                        request=request,
                    )
                    changed += 1
        except ValidationError as exc:
            # The transaction rolled back: nothing was saved. Show the errors on this page.
            for message in exc.messages:
                messages.error(request, message)
        except IntegrityError as exc:
            logger.warning("academics conflict: %s", type(exc).__name__)
            messages.error(request, CONFLICT_MESSAGE)
        else:
            messages.success(request, "Saved." if changed else "Nothing changed.")
            return redirect("academics:arm_detail", arm_id=arm.pk)

    existing = {o.subject_id: o for o in SubjectOffering.objects.filter(arm=arm, session=session)}
    form_teacher = FormTeacher.objects.filter(arm=arm, session=session).first()
    rows = []
    for subject in subjects_here:
        off = existing.get(subject.pk)
        rows.append(
            {
                "subject": subject,
                "teacher_id": off.teacher_id if off else None,
                "offering_id": off.pk if off else None,
                "is_compulsory": off.is_compulsory if off else True,
            }
        )
    ctx.update(
        teachers=teachers,
        rows=rows,
        form_teacher=form_teacher,
        form_teacher_id=form_teacher.teacher_id if form_teacher else None,
    )
    return render(request, "academics/arm_detail.html", ctx)


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL)
def settings_page(request):
    can_edit = request.user.has_role(SCHOOL_ADMIN)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        form = AdmissionSettingsForm(request.POST)
        if form.is_valid() and _save(request, form, lambda c: "Admission number settings saved."):
            return redirect("academics:settings")
    else:
        conf = academic_settings_or_defaults()
        form = AdmissionSettingsForm(
            initial={
                "admission_prefix": conf.admission_prefix,
                "admission_pattern": conf.admission_pattern,
            }
        )
    sample = preview_next_admission_number(timezone.localdate().year)
    return render(
        request,
        "academics/settings.html",
        {"form": form, "sample": sample, "can_edit": can_edit},
    )
