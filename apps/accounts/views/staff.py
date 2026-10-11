from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from apps.accounts.models import User
from apps.accounts.roles import PROPRIETOR, SCHOOL_ADMIN, STAFF_ROLES, require_roles
from apps.accounts.services import (
    deactivate_staff,
    invite_staff,
    reactivate_staff,
    resend_invite,
    reset_mfa,
    update_staff_roles,
)

ROLE_LABELS = {
    "proprietor": "Proprietor",
    "school_admin": "School administrator",
    "principal": "Principal",
    "bursar": "Bursar",
    "teacher": "Teacher",
}
ROLE_CHOICES = [(r, ROLE_LABELS[r]) for r in STAFF_ROLES]


class InviteForm(forms.Form):
    full_name = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=20, required=False, label="Phone")
    roles = forms.MultipleChoiceField(
        choices=ROLE_CHOICES, widget=forms.CheckboxSelectMultiple, required=False
    )

    def clean(self):
        data = super().clean()
        if not data.get("email") and not data.get("phone") and not self.errors:
            raise ValidationError("Enter an email address or a phone number.")
        return data


class RolesForm(forms.Form):
    roles = forms.MultipleChoiceField(
        choices=ROLE_CHOICES, widget=forms.CheckboxSelectMultiple, required=False
    )


def _base_url(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def _attach(form, exc: ValidationError) -> None:
    if hasattr(exc, "error_dict"):
        for field, errs in exc.message_dict.items():
            for msg in errs:
                form.add_error(field if field in form.fields else None, msg)
    else:
        for msg in exc.messages:
            form.add_error(None, msg)


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR)
def staff_list_view(request):
    staff = User.objects.filter(kind=User.Kind.STAFF).prefetch_related("groups")
    rows = [
        {
            "user": u,
            "roles": [ROLE_LABELS.get(r, r) for r in sorted(u.roles)],
            "pending": not u.has_usable_password(),
        }
        for u in staff.order_by("first_name", "last_name", "pk")
    ]
    return render(request, "accounts/staff_list.html", {"rows": rows})


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR)
def staff_invite_view(request):
    form = InviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            invite_staff(
                full_name=d["full_name"],
                email=d["email"],
                phone=d["phone"],
                roles=d["roles"],
                by=request.user,
                base_url=_base_url(request),
                request=request,
            )
        except ValidationError as exc:
            _attach(form, exc)
        else:
            messages.success(request, "Invite sent.")
            return redirect(reverse("staff:list"))
    return render(request, "accounts/staff_form.html", {"form": form, "invite": True})


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR)
def staff_edit_view(request, pk: int):
    target = get_object_or_404(User, pk=pk, kind=User.Kind.STAFF)
    roles_form = RolesForm(initial={"roles": sorted(target.roles)})
    errors: list[str] = []

    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            with transaction.atomic():
                if action == "roles":
                    roles_form = RolesForm(request.POST)
                    if not roles_form.is_valid():
                        raise ValidationError("Choose valid roles.")
                    update_staff_roles(
                        target, roles_form.cleaned_data["roles"], by=request.user, request=request
                    )
                    messages.success(request, "Roles updated.")
                elif action == "deactivate":
                    deactivate_staff(target, by=request.user, request=request)
                    messages.success(request, "Account deactivated.")
                elif action == "reactivate":
                    reactivate_staff(target, by=request.user, request=request)
                    messages.success(request, "Account reactivated.")
                elif action == "resend":
                    resend_invite(
                        target, by=request.user, base_url=_base_url(request), request=request
                    )
                    messages.success(request, "Invite sent again.")
                elif action == "reset_mfa":
                    reset_mfa(target, by=request.user, request=request)
                    messages.success(
                        request, "Two-factor reset. They will set it up at next login."
                    )
                else:
                    raise Http404
        except ValidationError as exc:
            errors = exc.messages
            target.refresh_from_db()
            roles_form = RolesForm(initial={"roles": sorted(target.roles)})
        else:
            return redirect(reverse("staff:edit", args=[target.pk]))

    return render(
        request,
        "accounts/staff_form.html",
        {
            "target": target,
            "roles_form": roles_form,
            "errors": errors,
            "pending": not target.has_usable_password(),
        },
        status=200,
    )
