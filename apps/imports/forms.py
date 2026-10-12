import os

from django import forms

from apps.imports.models import ImportJob
from apps.imports.parsing import ALLOWED_EXTENSIONS, MAX_BYTES


class ImportForm(forms.Form):
    kind = forms.ChoiceField(
        choices=[(ImportJob.Kind.STUDENTS, "Students")], label="What are you importing?"
    )
    file = forms.FileField(
        label="File", help_text="A .csv or .xlsx file, at most 5 MB and 2,000 rows."
    )

    def clean_file(self):  # type: ignore[no-untyped-def]
        upload = self.cleaned_data["file"]
        ext = os.path.splitext(upload.name or "")[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError("Upload a .csv or .xlsx file.")
        if upload.size > MAX_BYTES:
            raise forms.ValidationError("The file is larger than 5 MB.")
        return upload
