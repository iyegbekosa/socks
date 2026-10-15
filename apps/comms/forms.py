import re
from typing import Any

from django import forms
from django.core.exceptions import ValidationError

from apps.academics.models import ClassArm, ClassLevel, Term
from apps.academics.services import current_session
from apps.accounts.identifiers import normalise_admission_number
from apps.comms.announcements import BODY_MAX, SUBJECT_MAX, students_for
from apps.comms.audience import MAX_STUDENTS, Audience
from apps.comms.render import check_template
from apps.core.money import kobo_from_naira
from apps.fees.forms import naira_text
from apps.students.models import Student

AUDIENCE_FIELDS = ("level", "arm", "term", "min_amount", "students")
AUDIENCE_CHOICES = [
    ("school", "The whole school"),
    ("level", "One class level"),
    ("arm", "One class"),
    ("debtors", "Students owing fees"),
    ("students", "Chosen students"),
]
SEPARATORS = re.compile(r"[,\r\n]+")


class ComposeForm(forms.Form):
    """An announcement: who it is for, the subject and the body. ``clean`` builds
    ``self.chosen_audience`` and resolves ``self.students_found`` (never empty when valid)."""

    token = forms.CharField(widget=forms.HiddenInput, required=False)
    audience = forms.ChoiceField(label="Send to", choices=AUDIENCE_CHOICES)
    level = forms.ModelChoiceField(
        label="Class level", queryset=ClassLevel.objects.none(), required=False
    )
    arm = forms.ModelChoiceField(label="Class", queryset=ClassArm.objects.none(), required=False)
    term = forms.ModelChoiceField(
        label="Term (students owing fees)",
        queryset=Term.objects.none(),
        required=False,
        empty_label="Current term",
    )
    min_amount = forms.CharField(
        label="Owing at least (₦)",
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )
    students = forms.CharField(
        label="Admission numbers (chosen students)",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=f"Separate with commas or new lines; at most {MAX_STUDENTS}.",
    )
    subject = forms.CharField(max_length=SUBJECT_MAX)
    body = forms.CharField(
        max_length=BODY_MAX,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="You may use {school_name}, {term} and {date}.",
    )

    def __init__(self, *args: Any, audiences: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["audience"].choices = [  # type: ignore[attr-defined]
            c for c in AUDIENCE_CHOICES if c[0] in audiences
        ]
        if "debtors" not in audiences:
            del self.fields["term"], self.fields["min_amount"]
        if "arm" not in audiences:  # a bursar: debtors only
            del self.fields["level"], self.fields["arm"], self.fields["students"]
        session = current_session()
        if "level" in self.fields:
            self.fields["level"].queryset = ClassLevel.objects.all()  # type: ignore[attr-defined]
            self.fields["arm"].queryset = ClassArm.objects.select_related("level")  # type: ignore[attr-defined]
        if "term" in self.fields:
            self.fields["term"].queryset = (  # type: ignore[attr-defined]
                Term.objects.filter(session=session).select_related("session")
                if session is not None
                else Term.objects.none()
            )
        self.chosen_audience: Audience | None = None
        self.students_found: list[Student] = []

    def clean_body(self) -> str:
        body = self.cleaned_data["body"]
        check_template(body, per_student=False)
        return body

    def clean_min_amount(self) -> int:
        raw = (self.cleaned_data.get("min_amount") or "").strip()
        if not raw:
            return 1
        try:
            return max(1, kobo_from_naira(naira_text(raw)))
        except (TypeError, ValueError):
            raise ValidationError(
                "Enter the minimum balance in naira, for example 10000."
            ) from None

    def clean_students(self) -> list[int]:
        if self.data.get("audience") != "students":
            return []
        numbers = list(
            dict.fromkeys(
                n
                for n in (
                    normalise_admission_number(part)
                    for part in SEPARATORS.split(self.cleaned_data.get("students") or "")
                )
                if n
            )
        )
        if not numbers:
            raise ValidationError("Enter at least one admission number.")
        if len(numbers) > MAX_STUDENTS:
            raise ValidationError(f"Enter at most {MAX_STUDENTS} admission numbers.")
        found = {
            s.user.admission_number: s
            for s in Student.objects.filter(user__admission_number__in=numbers).select_related(
                "user"
            )
        }
        bad = [n for n in numbers if n not in found or found[n].status != Student.Status.ACTIVE]
        if bad:
            raise ValidationError(f"Unknown or inactive admission numbers: {', '.join(bad)}.")
        return [found[n].pk for n in numbers]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        kind = data.get("audience")
        if kind is None or self.errors:
            return data
        if kind == "level" and data.get("level") is None:
            self.add_error("level", "Choose a class level.")
        elif kind == "arm" and data.get("arm") is None:
            self.add_error("arm", "Choose a class.")
        if self.errors:
            return data
        target = data.get(kind) if kind in ("level", "arm") else None
        term = data.get("term")
        audience = Audience(
            type=kind,
            id=target.pk if target is not None else None,
            ids=tuple(data.get("students") or ()),
            term=term.pk if kind == "debtors" and term is not None else None,
            min_balance_kobo=data.get("min_amount") or 1,
        )
        try:
            self.students_found = students_for(audience)
        except ValidationError as exc:
            self.add_error(None, exc)
            return data
        self.chosen_audience = audience
        return data
