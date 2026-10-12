from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.accounts.roles import BURSAR, PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, TEACHER


@login_required
def home(request):
    can_see_students = request.user.kind == "staff" and request.user.has_role(
        SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL, BURSAR, TEACHER
    )
    return render(request, "accounts/home.html", {"can_see_students": can_see_students})
