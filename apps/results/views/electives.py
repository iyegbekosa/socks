from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache

from apps.academics.models import FormTeacher, SubjectOffering
from apps.accounts.roles import SCHOOL_ADMIN, TEACHER, require_roles
from apps.core.parsing import parse_id
from apps.results.models import ElectiveChoice
from apps.results.services import set_electives
from apps.students.models import Enrolment, Student


def _can_manage(user, offering) -> bool:
    if user.has_role(SCHOOL_ADMIN):
        return True
    return FormTeacher.objects.filter(
        arm_id=offering.arm_id, session_id=offering.session_id, teacher=user
    ).exists()


@never_cache
@require_roles(SCHOOL_ADMIN, TEACHER)
def electives_page(request, offering_pk):
    offering = get_object_or_404(
        SubjectOffering.objects.select_related("subject", "arm__level", "session"), pk=offering_pk
    )
    if not _can_manage(request.user, offering):
        raise PermissionDenied
    ctx = {"offering": offering, "arm": offering.arm}
    if offering.is_compulsory:
        return render(request, "results/electives.html", ctx)

    students = list(
        Student.objects.filter(
            status=Student.Status.ACTIVE,
            enrolments__arm_id=offering.arm_id,
            enrolments__session_id=offering.session_id,
            enrolments__status=Enrolment.Status.ACTIVE,
        ).order_by("last_name", "first_name", "pk")
    )
    chosen = set(
        ElectiveChoice.objects.filter(offering=offering).values_list("student_id", flat=True)
    )
    if request.method == "POST":
        parsed = [parse_id(i) for i in request.POST.getlist("students")]
        posted = [i for i in parsed if i is not None]
        try:
            if len(posted) != len(parsed):
                raise ValidationError("Choose only students from the list.")
            set_electives(offering, posted, by=request.user, request=request)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        except IntegrityError:
            messages.error(
                request, "Someone else changed this at the same time — please try again."
            )
        else:
            messages.success(request, "Saved.")
            return redirect("results:electives", offering_pk=offering.pk)
        chosen = set(posted)
    ctx.update(rows=[{"student": s, "checked": s.pk in chosen} for s in students])
    return render(request, "results/electives.html", ctx)
