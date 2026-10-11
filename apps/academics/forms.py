from django import forms
from django.core.exceptions import ValidationError

from apps.academics.models import SECTION_CHOICES, STREAM_CHOICES, ClassLevel
from apps.academics.services import (
    create_arm,
    create_session,
    create_subject,
    rename_arm,
    update_subject,
)

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


def _push_errors(form: forms.Form, exc: ValidationError) -> None:
    if hasattr(exc, "error_dict"):
        for key, messages in exc.message_dict.items():
            for message in messages:
                form.add_error(key if key in form.fields else None, message)
    else:
        for message in exc.messages:
            form.add_error(None, message)


class ArmForm(forms.Form):
    level = forms.ModelChoiceField(queryset=ClassLevel.objects.none())
    suffix = forms.CharField(max_length=40, label="Arm", help_text="For example A, B or Gold")
    stream = forms.ChoiceField(
        choices=[("", "No stream"), *STREAM_CHOICES], required=False, label="Stream (SSS only)"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["level"].queryset = ClassLevel.objects.all()

    def save(self, *, by, request=None):
        data = self.cleaned_data
        try:
            return create_arm(
                level=data["level"],
                suffix=data["suffix"],
                stream=data["stream"] or None,
                by=by,
                request=request,
            )
        except ValidationError as exc:
            _push_errors(self, exc)
            return None


class ArmRenameForm(forms.Form):
    suffix = forms.CharField(max_length=40, label="Arm")
    stream = forms.ChoiceField(
        choices=[("", "No stream"), *STREAM_CHOICES], required=False, label="Stream (SSS only)"
    )

    def __init__(self, *args, arm, **kwargs):
        self.arm = arm
        kwargs.setdefault("initial", {"suffix": arm.suffix, "stream": arm.stream or ""})
        super().__init__(*args, **kwargs)

    def save(self, *, by, request=None):
        data = self.cleaned_data
        try:
            return rename_arm(
                self.arm,
                suffix=data["suffix"],
                stream=data["stream"] or None,
                by=by,
                request=request,
            )
        except ValidationError as exc:
            _push_errors(self, exc)
            return None


class SubjectForm(forms.Form):
    name = forms.CharField(max_length=80, label="Subject name")
    sections = forms.MultipleChoiceField(
        choices=SECTION_CHOICES, widget=forms.CheckboxSelectMultiple, label="Taught in"
    )

    def __init__(self, *args, subject=None, **kwargs):
        self.subject = subject
        if subject is not None:
            kwargs.setdefault("initial", {"name": subject.name, "sections": subject.sections})
        super().__init__(*args, **kwargs)

    def save(self, *, by, request=None):
        data = self.cleaned_data
        try:
            if self.subject is None:
                return create_subject(
                    name=data["name"], sections=data["sections"], by=by, request=request
                )
            return update_subject(
                self.subject, name=data["name"], sections=data["sections"], by=by, request=request
            )
        except ValidationError as exc:
            _push_errors(self, exc)
            return None
