import os

from django import forms
from django.db import transaction
from PIL import Image, UnidentifiedImageError

from apps.branding.models import COLOR_VALIDATOR, Branding
from apps.tenancy.limits import PlanLimitExceeded

MAX_LOGO_BYTES = 1_048_576
LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class BrandingForm(forms.ModelForm):
    primary_color = forms.CharField(max_length=7, validators=[COLOR_VALIDATOR])
    accent_color = forms.CharField(max_length=7, validators=[COLOR_VALIDATOR])

    class Meta:
        model = Branding
        fields = ["motto", "address", "primary_color", "accent_color", "logo"]
        widgets = {"logo": forms.FileInput}

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if not logo or not hasattr(logo, "size") or not hasattr(logo, "content_type"):
            return logo  # unchanged, or cleared
        ext = os.path.splitext(logo.name)[1].lower()
        if ext not in LOGO_EXTENSIONS:
            raise forms.ValidationError("Logo must be a .png, .jpg, .jpeg or .webp file.")
        if logo.size > MAX_LOGO_BYTES:
            raise forms.ValidationError("Logo must be 1 MB or smaller.")
        try:
            logo.seek(0)
            Image.open(logo).verify()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            raise forms.ValidationError("That file is not a valid image.") from None
        finally:
            logo.seek(0)
        return logo

    def save_with_quota(self):
        """Save, turning a storage-quota failure into a form error. Returns the instance or None."""
        try:
            with transaction.atomic():  # savepoint, so a quota failure leaves the txn usable
                return self.save()
        except PlanLimitExceeded as exc:
            self.add_error("logo", exc.message)
            return None
