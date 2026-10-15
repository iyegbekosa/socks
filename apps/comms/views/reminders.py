"""Fee reminder dates, minimum balance and template (``comms:reminders``), "Send reminders
now" and deleting a date. The bursar and school admin edit; the principal and proprietor read.
"""

import datetime

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.services import current_term
from apps.accounts.roles import BURSAR, PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN, require_roles
from apps.attendance.days import is_school_day
from apps.audit.services import record
from apps.comms import reminders
from apps.comms.models import DEFAULT_REMINDER_TEMPLATE, CommsSettings, FeeReminderDate
from apps.comms.render import allowed, check_template
from apps.core.locks import school_lock
from apps.core.money import kobo_from_naira
from apps.core.parsing import parse_id
from apps.fees.forms import naira_text

EDIT_ROLES = (BURSAR, SCHOOL_ADMIN)
VIEW_ROLES = (BURSAR, SCHOOL_ADMIN, PRINCIPAL, PROPRIETOR)
TEMPLATE_MAX = 1000


class DateForm(forms.Form):
    date = forms.DateField(label="Reminder date", widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, term, **kwargs):
        super().__init__(*args, **kwargs)
        self.term = term

    def clean_date(self) -> datetime.date:
        value = self.cleaned_data["date"]
        if value < timezone.localdate():
            raise ValidationError("That date is in the past.")
        if not is_school_day(self.term, value):
            raise ValidationError("Choose a school day in the current term.")
        if FeeReminderDate.objects.filter(term=self.term, date=value).exists():
            raise ValidationError("That date is already a reminder date.")
        return value


class SettingsForm(forms.Form):
    min_balance = forms.CharField(
        label="Remind families owing at least (₦)",
        max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )
    template = forms.CharField(
        label="Reminder text",
        max_length=TEMPLATE_MAX,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Use only: " + ", ".join(f"{{{p}}}" for p in allowed(True)) + ".",
    )

    def clean_min_balance(self) -> int:
        try:
            return kobo_from_naira(naira_text(self.cleaned_data["min_balance"]))
        except (TypeError, ValueError):
            raise ValidationError("Enter the minimum balance in naira, for example 1000.") from None

    def clean_template(self) -> str:
        body = self.cleaned_data["template"].strip()
        if not body:
            raise ValidationError("Write the reminder text.")
        check_template(body, per_student=True)
        return body


def _page(request, *, date_form=None, settings_form=None):
    term = current_term()
    conf = CommsSettings.load()
    dates = list(FeeReminderDate.objects.filter(term=term)) if term else []
    today = timezone.localdate()
    editable = request.user.has_role(*EDIT_ROLES)
    return render(
        request,
        "comms/reminders.html",
        {
            "term": term,
            "dates": [{"obj": d, "deletable": d.date >= today} for d in dates],
            "date_form": date_form or (DateForm(term=term) if term else None),
            "settings_form": settings_form
            or SettingsForm(
                initial={
                    "min_balance": f"{conf.reminder_min_balance_kobo / 100:.2f}",
                    "template": conf.reminder_template,
                }
            ),
            "max_dates": reminders.MAX_DATES,
            "full": len(dates) >= reminders.MAX_DATES,
            "editable": editable,
        },
    )


@never_cache
@require_roles(*VIEW_ROLES)
def reminders_page(request):
    if request.method != "POST":
        return _page(request)
    if not request.user.has_role(*EDIT_ROLES):
        raise PermissionDenied
    action = request.POST.get("action", "")
    if action == "add_date":
        return _add_date(request)
    if action == "save_settings":
        return _save_settings(request)
    if action == "reset_template":
        return _reset_template(request)
    messages.error(request, "Choose what to do.")
    return redirect("comms:reminders")


REMINDER_HOUR = 8  # the daily Beat run (config/settings.py, "comms-fee-reminders")


def _past_reminder_hour() -> bool:
    return timezone.localtime().hour >= REMINDER_HOUR


def _add_date(request):
    term = current_term()
    if term is None:
        messages.error(request, "There is no current term.")
        return redirect("comms:reminders")
    form = DateForm(request.POST, term=term)
    with transaction.atomic():
        school_lock("comms:reminder-dates")
        if FeeReminderDate.objects.filter(term=term).count() >= reminders.MAX_DATES:
            form.add_error(None, f"A term can have at most {reminders.MAX_DATES} reminder dates.")
        elif form.is_valid():
            row = FeeReminderDate.objects.create(term=term, date=form.cleaned_data["date"])
            record(
                "comms.reminder_date_saved",
                request=request,
                obj=row,
                after={"id": row.pk, "date": row.date.isoformat()},
            )
            messages.success(request, "Reminder date added.")
            if row.date == timezone.localdate() and _past_reminder_hour():
                messages.info(
                    request,
                    "Today's date was added after 08:00, so the scheduled reminder will not "
                    "run today. Use Send now.",
                )
            return redirect("comms:reminders")
    return _page(request, date_form=form)


def _save_settings(request):
    form = SettingsForm(request.POST)
    if not form.is_valid():
        return _page(request, settings_form=form)
    _apply(request, form.cleaned_data["min_balance"], form.cleaned_data["template"])
    messages.success(request, "Reminder settings saved.")
    return redirect("comms:reminders")


def _reset_template(request):
    _apply(request, None, DEFAULT_REMINDER_TEMPLATE)
    messages.success(request, "Reminder text reset to the default.")
    return redirect("comms:reminders")


def _apply(request, min_kobo: int | None, template: str) -> None:
    with transaction.atomic():
        conf = CommsSettings.objects.select_for_update().get(pk=CommsSettings.load().pk)
        before: dict = {}
        after: dict = {}
        if min_kobo is not None and min_kobo != conf.reminder_min_balance_kobo:
            before["reminder_min_balance_kobo"] = conf.reminder_min_balance_kobo
            after["reminder_min_balance_kobo"] = min_kobo
            conf.reminder_min_balance_kobo = min_kobo
        if template != conf.reminder_template:
            after["template_changed"] = True  # never the text itself
            conf.reminder_template = template
        if not after:
            return
        conf.save()
        record(
            "comms.reminder_settings_saved", request=request, obj=conf, before=before, after=after
        )


@require_POST
@require_roles(*EDIT_ROLES)
def reminder_date_delete(request, pk):
    row_id = parse_id(pk)
    row = FeeReminderDate.objects.filter(pk=row_id).first() if row_id is not None else None
    if row is None:
        messages.error(request, "That reminder date no longer exists.")
        return redirect("comms:reminders")
    if row.date < timezone.localdate():
        messages.error(request, "A past reminder date cannot be removed.")
        return redirect("comms:reminders")
    with transaction.atomic():
        record(
            "comms.reminder_date_deleted",
            request=request,
            obj=row,
            after={"id": row.pk, "date": row.date.isoformat()},
        )
        row.delete()
    messages.success(request, "Reminder date removed.")
    return redirect("comms:reminders")


@require_POST
@require_roles(*EDIT_ROLES)
def reminders_send(request):
    message = reminders.send_now(timezone.localdate(), by=request.user)
    if message is None:
        messages.info(
            request,
            "No reminder was sent: nobody owes the minimum balance, or today's reminder has "
            "already gone out.",
        )
    else:
        messages.success(request, "Reminders sent.")
    return redirect("comms:reminders")
