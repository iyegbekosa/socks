from django import forms

from apps.attendance.models import AttendanceRecord, AttendanceSettings, Holiday

CONFLICT = "A register was already taken on this date."


class AttendanceSettingsForm(forms.ModelForm):
    class Meta:
        model = AttendanceSettings
        fields = ["late_threshold", "edit_window_days", "absence_email"]
        labels = {
            "late_threshold": "Late threshold",
            "edit_window_days": "Form teacher edit window (days)",
            "absence_email": "Email guardians when a student is absent",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("late_threshold", "edit_window_days"):
            self.fields[name].widget.attrs.update({"min": 0, "max": 60})


class HolidayForm(forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def clean_date(self):
        d = self.cleaned_data["date"]
        if AttendanceRecord.objects.filter(date=d).exists():
            raise forms.ValidationError(CONFLICT)
        return d
