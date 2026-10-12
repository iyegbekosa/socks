from django import forms
from django.core.exceptions import ValidationError

from apps.academics.models import ClassArm
from apps.students.models import RELATIONSHIP_CHOICES, Student
from apps.students.services import GuardianData

DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d"]
BLANK = [("", "Choose…")]


def _date_field(label: str) -> forms.DateField:
    return forms.DateField(
        label=label,
        input_formats=DATE_FORMATS,
        widget=forms.DateInput(
            format="%d/%m/%Y", attrs={"placeholder": "DD/MM/YYYY", "autocomplete": "off"}
        ),
        help_text="Day/month/year, for example 25/12/2014.",
    )


class ArmChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: ClassArm) -> str:
        return obj.display_name


class StudentProfileForm(forms.Form):
    """Profile and guardian fields shared by admission and editing."""

    first_name = forms.CharField(max_length=80)
    middle_name = forms.CharField(max_length=80, required=False)
    last_name = forms.CharField(max_length=80)
    sex = forms.ChoiceField(choices=BLANK + list(Student.Sex.choices))
    date_of_birth = _date_field("Date of birth")

    guardian_name = forms.CharField(max_length=120, label="Main guardian's name")
    guardian_relationship = forms.ChoiceField(
        choices=BLANK + RELATIONSHIP_CHOICES, label="Relationship"
    )
    guardian_phone = forms.CharField(
        max_length=20, required=False, label="Phone", widget=forms.TextInput(attrs={"type": "tel"})
    )
    guardian_email = forms.EmailField(
        required=False,
        label="Email",
        help_text="Strongly recommended: password reset links are sent here.",
    )

    guardian2_name = forms.CharField(max_length=120, required=False, label="Second guardian's name")
    guardian2_relationship = forms.ChoiceField(
        choices=[("", "—")] + RELATIONSHIP_CHOICES, required=False, label="Relationship"
    )
    guardian2_phone = forms.CharField(
        max_length=20, required=False, label="Phone", widget=forms.TextInput(attrs={"type": "tel"})
    )
    guardian2_email = forms.EmailField(required=False, label="Email")

    GUARDIAN1 = ("guardian_name", "guardian_relationship", "guardian_phone", "guardian_email")
    GUARDIAN2 = ("guardian2_name", "guardian2_relationship", "guardian2_phone", "guardian2_email")

    def bound(self, names: tuple[str, ...]) -> list[forms.BoundField]:
        return [self[n] for n in names]

    @property
    def student_fields(self) -> list[forms.BoundField]:
        return self.bound(("first_name", "middle_name", "last_name", "sex", "date_of_birth"))

    @property
    def guardian1_fields(self) -> list[forms.BoundField]:
        return self.bound(self.GUARDIAN1)

    @property
    def guardian2_fields(self) -> list[forms.BoundField]:
        return self.bound(self.GUARDIAN2)

    def guardians(self) -> tuple[GuardianData, GuardianData]:
        d = self.cleaned_data
        first = GuardianData(
            d["guardian_name"],
            d["guardian_relationship"],
            d["guardian_phone"] or None,
            d["guardian_email"],
        )
        second = GuardianData(
            d["guardian2_name"],
            d["guardian2_relationship"],
            d["guardian2_phone"] or None,
            d["guardian2_email"],
        )
        return first, second


class AdmitForm(StudentProfileForm):
    arm = ArmChoiceField(queryset=ClassArm.objects.none(), label="Class", empty_label="Choose…")
    admission_number = forms.CharField(
        max_length=40,
        required=False,
        help_text="Leave blank to generate the next number automatically.",
    )
    consent = forms.BooleanField(
        label="The guardian has given consent for the school to keep this student's records.",
        error_messages={"required": "Record the guardian's consent before admitting."},
    )
    consent_method = forms.ChoiceField(
        choices=BLANK + list(Student.ConsentMethod.choices), label="How was consent given?"
    )

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.fields["arm"].queryset = ClassArm.objects.select_related("level")


class EditForm(StudentProfileForm):
    photo = forms.FileField(required=False, label="Replace photo")


def attach_errors(form: forms.Form, exc: ValidationError) -> None:
    """Map a service ValidationError onto form fields (unknown keys become form errors)."""
    if hasattr(exc, "error_dict"):
        for key, messages in exc.message_dict.items():
            for msg in messages:
                form.add_error(key if key in form.fields else None, msg)
    else:
        for msg in exc.messages:
            form.add_error(None, msg)
