from typing import Any

from django.db import transaction

from apps.attendance.forms import CONFLICT, HolidayForm
from apps.attendance.models import AttendanceRecord, AttendanceSettings, Holiday
from apps.audit.services import record
from apps.core.locks import school_lock

FIELDS = ("late_threshold", "edit_window_days", "absence_email", "absence_email_delay_minutes")
DUPLICATE = "A holiday is already set on this date."


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
        d = form.cleaned_data["date"]
        if AttendanceRecord.objects.filter(date=d).exists():
            form.add_error("date", CONFLICT)
            return None
        # Another admin may have added the same date since this form was validated.
        if Holiday.objects.filter(date=d).exists():
            form.add_error("date", DUPLICATE)
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


def delete_holiday(pk: int, *, by: Any, request: Any = None) -> bool:
    """Delete holiday ``pk``; False if it no longer exists. Always safe for card totals: no
    register can exist on a holiday's date (a holiday is refused on a date with records, and
    a register is refused on a holiday)."""
    with transaction.atomic():
        school_lock("attendance:holidays")
        holiday = Holiday.objects.filter(pk=pk).first()
        if holiday is None:
            return False
        after = {"holiday_id": holiday.pk, "date": holiday.date.isoformat()}
        record("attendance.holiday_deleted", request=request, actor=by, obj=holiday, after=after)
        holiday.delete()
    return True
