from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.forms import SessionForm
from apps.academics.models import AcademicSession, Term
from apps.academics.services import set_current_term
from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles


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
            session = form.save(by=request.user, request=request)
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
    set_current_term(term, by=request.user, request=request)
    messages.success(request, f"{term} is now the current term.")
    return redirect("academics:sessions")
