"""Student admin pages. Children's data: nothing here logs DOB, contacts, photos or passwords."""

import time
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.models import ClassArm
from apps.academics.scope import arms_taught_by, students_visible_to, teacher_students
from apps.academics.services import current_session
from apps.accounts.roles import (
    BURSAR,
    PRINCIPAL,
    PROPRIETOR,
    SCHOOL_ADMIN,
    STAFF_ROLES,
    TEACHER,
    require_roles,
)
from apps.students.forms import AdmitForm, EditForm, attach_errors
from apps.students.models import Enrolment, Student
from apps.students.passwords import issue_one_time_passwords
from apps.students.services import (
    admit_student,
    is_admission_number_clash,
    set_student_photo,
    update_student,
    withdraw_student,
)
from apps.tenancy.limits import PlanLimitExceeded

PAGE_SIZE = 50
JUST_ADMITTED = "students:just_admitted"
PASSWORD_TTL_SECONDS = 300
FULL_VIEW_ROLES = (SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL)


def _visible_student(request: Any, pk: int) -> Student:
    return get_object_or_404(
        students_visible_to(request.user).select_related("user", "consent_recorded_by"), pk=pk
    )


def _enrolment_for_display(student: Student, session: Any) -> Enrolment | None:
    """The current session's enrolment, else the most recent one (needs prefetched rows)."""
    rows = list(student.enrolments.all())
    if not rows:
        return None
    for row in rows:
        if session is not None and row.session_id == session.pk:
            return row
    return max(rows, key=lambda r: r.session.start_date)


def _enrolment_prefetch() -> Prefetch:
    return Prefetch("enrolments", Enrolment.objects.select_related("arm__level", "session"))


def _can_see_photo(user: Any, student: Student) -> bool:
    if user.has_role(*FULL_VIEW_ROLES):
        return True
    return user.has_role(TEACHER) and teacher_students(user).filter(pk=student.pk).exists()


def _fresh(entry: Any, now: float) -> bool:
    try:
        return now - float(entry["at"]) <= PASSWORD_TTL_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_roles(*STAFF_ROLES)
def student_list(request):
    qs = students_visible_to(request.user).select_related("user")
    q = (request.GET.get("q") or "").strip()
    for token in q.split():
        qs = qs.filter(
            Q(first_name__icontains=token)
            | Q(middle_name__icontains=token)
            | Q(last_name__icontains=token)
            | Q(user__admission_number__icontains=token)
        )
    arm_id = (request.GET.get("arm") or "").strip()
    session = current_session()  # once per request
    if request.user.has_role(*FULL_VIEW_ROLES, BURSAR):
        arms = ClassArm.objects.select_related("level")
    else:
        arms = arms_taught_by(request.user, session).select_related("level")
    if arm_id.isascii() and arm_id.isdigit() and session is not None:
        qs = qs.filter(enrolments__arm_id=int(arm_id), enrolments__session=session).distinct()
    page = Paginator(qs.prefetch_related(_enrolment_prefetch()), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    rows = [
        {"student": s, "enrolment": _enrolment_for_display(s, session)} for s in page.object_list
    ]
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "students/list.html",
        {
            "rows": rows,
            "page": page,
            "q": q,
            "arm_id": arm_id,
            "arms": arms,
            "querystring": query.urlencode(),
            "can_admit": request.user.has_role(SCHOOL_ADMIN),
            "can_print_passwords": request.user.has_role(SCHOOL_ADMIN)
            and arm_id.isdigit()
            and session is not None,
        },
    )


@never_cache
@require_roles(*STAFF_ROLES)
def detail(request, pk: int):
    student = get_object_or_404(
        students_visible_to(request.user)
        .select_related("user", "consent_recorded_by")
        .prefetch_related(_enrolment_prefetch()),
        pk=pk,
    )
    user = request.user
    full = user.has_role(*FULL_VIEW_ROLES)
    return render(
        request,
        "students/detail.html",
        {
            "student": student,
            "enrolment": _enrolment_for_display(student, current_session()),
            "show_emails": user.has_role(*FULL_VIEW_ROLES, BURSAR),
            "show_private": full,  # date of birth and consent details
            "show_photo": bool(student.photo) and _can_see_photo(user, student),
            "can_edit": user.has_role(SCHOOL_ADMIN),
            "can_issue_password": user.has_role(SCHOOL_ADMIN)
            and student.status == Student.Status.ACTIVE,
            "can_withdraw": user.has_role(SCHOOL_ADMIN)
            and student.status != Student.Status.WITHDRAWN,
        },
    )


@never_cache
@require_roles(SCHOOL_ADMIN)
def admit(request):
    session = current_session()
    if session is None:
        return render(request, "students/admit.html", {"no_session": True})
    form = AdmitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        guardian, guardian2 = form.guardians()
        has_second = any((guardian2.name, guardian2.relationship, guardian2.phone, guardian2.email))
        try:
            student, password = admit_student(
                first_name=d["first_name"],
                middle_name=d["middle_name"],
                last_name=d["last_name"],
                sex=d["sex"],
                date_of_birth=d["date_of_birth"],
                arm=d["arm"],
                session=session,
                guardian=guardian,
                guardian2=guardian2 if has_second else None,
                consent_method=d["consent_method"],
                admission_number=d["admission_number"],
                by=request.user,
                request=request,
            )
        except ValidationError as exc:
            attach_errors(form, exc)
        except PlanLimitExceeded as exc:
            form.add_error(None, exc.message)
        except IntegrityError as exc:
            if not is_admission_number_clash(exc):
                raise
            form.add_error("admission_number", "That admission number is already in use.")
        else:
            stash = request.session.get(JUST_ADMITTED)
            stash = stash if isinstance(stash, dict) else {}
            now = time.time()
            stash = {k: v for k, v in stash.items() if _fresh(v, now)}  # drop stale entries
            stash[str(student.pk)] = {"password": password, "at": now}
            request.session[JUST_ADMITTED] = stash
            return redirect("students:admitted", pk=student.pk)
    return render(request, "students/admit.html", {"form": form}, status=200)


@require_roles(SCHOOL_ADMIN)
def admitted(request, pk: int):
    """One-time confirmation: the password is popped from the session on first view."""
    student = get_object_or_404(Student.objects.select_related("user"), pk=pk)
    stash = request.session.get(JUST_ADMITTED)
    stash = stash if isinstance(stash, dict) else {}
    entry = stash.pop(str(student.pk), None)
    if stash:
        request.session[JUST_ADMITTED] = stash
    else:
        request.session.pop(JUST_ADMITTED, None)
    if not isinstance(entry, dict):
        return redirect("students:detail", pk=student.pk)
    if not _fresh(entry, time.time()):
        response = render(request, "students/admitted.html", {"student": student, "expired": True})
        response["Referrer-Policy"] = "no-referrer"
        return _no_store(response)
    enrolment = student.enrolments.select_related("arm__level").first()
    response = render(
        request,
        "students/admitted.html",
        {"student": student, "enrolment": enrolment, "password": entry.get("password")},
    )
    response["Referrer-Policy"] = "no-referrer"
    return _no_store(response)


@never_cache
@require_roles(*STAFF_ROLES)
def edit(request, pk: int):
    student = _visible_student(request, pk)
    if not request.user.has_role(SCHOOL_ADMIN):
        raise PermissionDenied
    initial = {f: getattr(student, f) for f in (*EditForm.base_fields.keys(),) if f != "photo"}
    form = EditForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        guardian, guardian2 = form.guardians()
        data = {f: d[f] for f in ("first_name", "middle_name", "last_name", "sex", "date_of_birth")}
        data["guardian"] = guardian
        data["guardian2"] = guardian2
        try:
            update_student(student, data, by=request.user, request=request)
            if d.get("photo"):
                set_student_photo(student, d["photo"], by=request.user, request=request)
        except ValidationError as exc:
            attach_errors(form, exc)
        except PlanLimitExceeded as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Student updated.")
            return redirect("students:detail", pk=student.pk)
    return render(request, "students/edit.html", {"form": form, "student": student})


@require_roles(*STAFF_ROLES)
@require_POST
def withdraw(request, pk: int):
    student = _visible_student(request, pk)
    if not request.user.has_role(SCHOOL_ADMIN):
        raise PermissionDenied
    withdraw_student(student, by=request.user, request=request)
    messages.success(request, "Student withdrawn.")
    return redirect("students:detail", pk=student.pk)


@require_roles(*STAFF_ROLES)
def photo(request, pk: int):
    if request.method != "GET":
        return HttpResponse(status=405, headers={"Allow": "GET"})
    student = _visible_student(request, pk)
    if not student.photo or not _can_see_photo(request.user, student):
        raise Http404
    return _no_store(HttpResponseRedirect(default_storage.url(student.photo.name)))


@require_roles(SCHOOL_ADMIN)
@require_POST
def passwords(request):
    """Issue new one-time passwords for chosen students or a whole class and render the sheet.

    The passwords exist only in this response: nothing is stored or flashed.
    """
    qs = active_password_candidates(request.user)
    arm_id = (request.POST.get("arm") or "").strip()
    ids = [
        int(v) for v in request.POST.getlist("student_ids") if v.isascii() and v.strip().isdigit()
    ]
    session = current_session()
    if arm_id.isascii() and arm_id.isdigit():
        if session is None:
            messages.error(request, "Set a current term first.")
            return redirect("students:list")
        qs = qs.filter(
            enrolments__arm_id=int(arm_id),
            enrolments__session=session,
            enrolments__status=Enrolment.Status.ACTIVE,
        )
    elif ids:
        qs = qs.filter(pk__in=ids)
    else:
        messages.error(request, "Choose a class or at least one student.")
        return redirect("students:list")
    return issue_and_render_sheet(request, qs, back="students:list")


def active_password_candidates(user: Any):
    """Students ``user`` may issue passwords for: visible to them, active, login enabled."""
    return students_visible_to(user).filter(status=Student.Status.ACTIVE, user__is_active=True)


def issue_and_render_sheet(request: Any, qs: Any, *, back: str) -> HttpResponse:
    """Issue one-time passwords for ``qs`` and render the printable sheet (shared with
    imports). ``qs`` must already be scoped with ``active_password_candidates``. The
    passwords exist only in this response: nothing is stored or flashed."""
    chosen = list(
        qs.select_related("user")
        .prefetch_related(_enrolment_prefetch())
        .distinct()
        .order_by("last_name", "first_name", "pk")
    )
    if not chosen:
        messages.error(request, "No active students matched.")
        return redirect(back)
    session = current_session()
    issued = issue_one_time_passwords(chosen, by=request.user, request=request)
    rows = [
        {"issued": item, "enrolment": _enrolment_for_display(item.student, session)}
        for item in issued
    ]
    response = render(
        request,
        "students/passwords.html",
        {
            "rows": rows,
            "portal_url": request.build_absolute_uri(reverse("accounts:login")),
        },
    )
    response["Referrer-Policy"] = "no-referrer"
    return _no_store(response)
