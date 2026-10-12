from django import forms

from apps.imports.models import ImportJob
from apps.imports.services import validate_upload


class ImportForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[(ImportJob.Kind.STUDENTS, "Students"), (ImportJob.Kind.STAFF, "Staff")],
        label="What are you importing?",
    )
    file = forms.FileField(
        label="File", help_text="A .csv or .xlsx file, at most 5 MB and 2,000 rows."
    )

    def clean_file(self):  # type: ignore[no-untyped-def]
        upload = self.cleaned_data["file"]
        validate_upload(upload)
        return upload
