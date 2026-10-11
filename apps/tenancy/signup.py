"""Signup form and subdomain rules for self-service school registration."""

import re

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django_tenants.utils import schema_exists

from apps.accounts.identifiers import normalise_ng_phone
from apps.tenancy.models import Domain, Plan, School

RESERVED_SUBDOMAINS = frozenset(
    {
        "www",
        "api",
        "admin",
        "app",
        "mail",
        "static",
        "media",
        "public",
        "platform",
        "help",
        "support",
        "status",
        "information-schema",
    }  # fmt: skip
)
LEVEL_CHOICES = [
    ("nursery", "Nursery"),
    ("primary", "Primary"),
    ("jss", "Junior secondary (JSS)"),
    ("sss", "Senior secondary (SSS)"),
]
_SUBDOMAIN_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")


def schema_name_for(subdomain: str) -> str:
    return subdomain.replace("-", "_")


def validate_subdomain(value: str) -> str:
    sub = (value or "").strip().lower()
    if not 3 <= len(sub) <= 30:
        raise ValidationError("Use between 3 and 30 characters.")
    if not _SUBDOMAIN_RE.fullmatch(sub):
        raise ValidationError(
            "Use letters, numbers and hyphens only, starting with a letter and not ending "
            "with a hyphen."
        )
    if "--" in sub:
        raise ValidationError("Do not use two hyphens in a row.")
    if sub in RESERVED_SUBDOMAINS or sub.startswith("pg"):
        raise ValidationError("This web address is not available.")
    if (
        School.objects.filter(schema_name=schema_name_for(sub)).exists()
        or Domain.objects.filter(domain__iexact=f"{sub}.{settings.PLATFORM_DOMAIN}").exists()
        # Any schema already in the database (pg_catalog, information_schema, leftovers...):
        # a school must never be built inside a schema it did not create.
        or schema_exists(schema_name_for(sub))
    ):
        raise ValidationError("This web address is already taken.")
    return sub


class SignupForm(forms.Form):
    school_name = forms.CharField(max_length=200)
    subdomain = forms.CharField(max_length=30, help_text="Your school's address: name.<platform>")
    levels_offered = forms.MultipleChoiceField(
        choices=LEVEL_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Tick at least one level."},
    )
    contact_name = forms.CharField(max_length=200)
    contact_phone = forms.CharField(max_length=30)
    contact_email = forms.EmailField()
    plan = forms.ChoiceField(choices=[])
    accept_terms = forms.BooleanField(
        error_messages={"required": "You must accept the terms to continue."}
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].choices = [
            (p.code, p.name) for p in Plan.objects.filter(is_active=True).order_by("id")
        ]

    def clean_subdomain(self) -> str:
        return validate_subdomain(self.cleaned_data["subdomain"])

    def clean_contact_phone(self) -> str:
        try:
            return normalise_ng_phone(self.cleaned_data["contact_phone"])
        except ValueError:
            raise ValidationError("Enter a valid Nigerian phone number.") from None

    def clean_contact_email(self) -> str:
        return self.cleaned_data["contact_email"].strip().lower()
