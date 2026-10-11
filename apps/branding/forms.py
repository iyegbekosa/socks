import logging

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from apps.branding.models import COLOR_VALIDATOR, Branding, public_storage
from apps.core.images import clean_image
from apps.tenancy.limits import PlanLimitExceeded

logger = logging.getLogger(__name__)
MAX_LOGO_BYTES = 1_048_576
MAX_LOGO_SIDE = 4096  # pixels; a tiny compressed file can declare a huge canvas
EXT_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


def _delete_old_logo(storage, name: str) -> None:
    """Best-effort cleanup after commit; a storage failure must not turn a saved form into a 500."""
    try:
        storage.delete(name)
    except Exception:
        logger.exception("Could not delete replaced logo %s", name)


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
        clean = clean_image(
            logo,
            max_bytes=MAX_LOGO_BYTES,
            allowed=EXT_FORMATS,
            max_pixels=MAX_LOGO_SIDE * MAX_LOGO_SIDE,
            output="same",
            max_side=None,
            quality=90,
            label="Logo",
        )
        return SimpleUploadedFile(logo.name, clean.read(), content_type=logo.content_type)

    def save_with_quota(self):
        """Save, turning a storage-quota failure into a form error. Returns the instance or None."""
        old_name = Branding.objects.filter(pk=1).values_list("logo", flat=True).first() or ""
        try:
            with transaction.atomic():  # savepoint, so a quota failure leaves the txn usable
                instance = self.save()
                new_name = instance.logo.name or ""
                if old_name and old_name != new_name:
                    storage = public_storage()
                    transaction.on_commit(lambda: _delete_old_logo(storage, old_name))
                return instance
        except PlanLimitExceeded as exc:
            self.add_error("logo", exc.message)
            return None
