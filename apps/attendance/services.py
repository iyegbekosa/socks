from typing import Any

from django.db import transaction

from apps.attendance.forms import CONFLICT, HolidayForm
from apps.attendance.models import AttendanceRecord, AttendanceSettings, Holiday
from apps.audit.services import record
from apps.core.locks import school_lock

FIELDS = ("late_threshold", "edit_window_days", "absence_email")


def save_settings(values: dict[str, Any], *, by: Any, request: Any = None) -> AttendanceSettings:
    """Apply ``values``; audit only the changed fields, and nothing if none changed."""
    with transaction.atomic():
        school_lock("attendance:settings")
        conf = AttendanceSettings.load()
        changed = [f for f in FIELDS if f in values and getattr(conf, f) != values[f]]
        if not changed:
            return conf
        before = {f: getattr(conf, f) for f in changed}
        for f in changed:
            setattr(conf, f, values[f])
        conf.save(update_fields=changed)
        record(
            "attendance.settings_saved",
            request=request,
            actor=by,
            obj=conf,
            before=before,
            after={f: getattr(conf, f) for f in changed},
        )
    return conf


def save_holiday(form: HolidayForm, *, by: Any, request: Any = None) -> Holiday | None:
    """Save a validated form. Returns None (with a form error) if a register appeared meanwhile."""
    with transaction.atomic():
        school_lock("attendance:holidays")
        # Re-check under the lock so a register saved meanwhile cannot slip past the form.
        if AttendanceRecord.objects.filter(date=form.cleaned_data["date"]).exists():
            form.add_error("date", CONFLICT)
            return None
        holiday = form.save()
        record(
            "attendance.holiday_saved",
            request=request,
            actor=by,
            obj=holiday,
            after={"holiday_id": holiday.pk, "date": holiday.date.isoformat()},
        )
    return holiday
