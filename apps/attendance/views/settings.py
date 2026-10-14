from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from apps.accounts.roles import PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles
from apps.attendance import services
from apps.attendance.forms import AttendanceSettingsForm, HolidayForm
from apps.attendance.models import AttendanceSettings, Holiday

VIEW_ROLES = (SCHOOL_ADMIN, PRINCIPAL, PROPRIETOR)


def _can_edit(user) -> bool:
    return user.has_role(SCHOOL_ADMIN)


@never_cache
@require_roles(*VIEW_ROLES)
def settings_page(request):
    can_edit = _can_edit(request.user)
    conf = AttendanceSettings.load() if can_edit else _settings_or_defaults()
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        form = AttendanceSettingsForm(request.POST, instance=conf)
        if form.is_valid():
            services.save_settings(form.cleaned_data, by=request.user, request=request)
            messages.success(request, "Attendance settings saved.")
            return redirect("attendance:settings")
    else:
        form = AttendanceSettingsForm(instance=conf)
    return render(request, "attendance/settings.html", {"form": form, "can_edit": can_edit})


def _settings_or_defaults() -> AttendanceSettings:
    return AttendanceSettings.objects.filter(pk=1).first() or AttendanceSettings(pk=1)


@never_cache
@require_roles(*VIEW_ROLES)
def holidays(request):
    can_edit = _can_edit(request.user)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        form = HolidayForm(request.POST)
        if form.is_valid():
            if services.save_holiday(form, by=request.user, request=request):
                messages.success(request, "Holiday saved.")
                return redirect("attendance:holidays")
    else:
        form = HolidayForm()
    return render(
        request,
        "attendance/holidays.html",
        {"form": form, "can_edit": can_edit, "holidays": Holiday.objects.all()},
    )
