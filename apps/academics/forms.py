from django import forms
from django.core.exceptions import ValidationError

from apps.academics.services import create_session

DATE_PLACEHOLDER = {"placeholder": "DD/MM/YYYY", "inputmode": "numeric"}


class SessionForm(forms.Form):
    name = forms.CharField(max_length=9, label="Session name", help_text="For example 2026/2027")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for i in (1, 2, 3):
            for part in ("start", "end"):
                self.fields[f"term_{i}_{part}"] = forms.DateField(
                    label=f"Term {i} {part}",
                    widget=forms.TextInput(attrs=DATE_PLACEHOLDER),
                )

    def term_rows(self):
        return [(i, self[f"term_{i}_start"], self[f"term_{i}_end"]) for i in (1, 2, 3)]

    def save(self, *, by, request=None):
        data = self.cleaned_data
        terms = [(data[f"term_{i}_start"], data[f"term_{i}_end"]) for i in (1, 2, 3)]
        try:
            return create_session(name=data["name"], terms=terms, by=by, request=request)
        except ValidationError as exc:
            for key, messages in exc.message_dict.items():
                if key in ("terms",):
                    key = None
                elif key.startswith("term_"):
                    key = f"{key}_end"
                for message in messages:
                    self.add_error(key, message)
            return None
