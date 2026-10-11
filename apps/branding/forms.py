import io
import logging
import os

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from PIL import Image, UnidentifiedImageError

from apps.branding.models import COLOR_VALIDATOR, Branding, public_storage
from apps.tenancy.limits import PlanLimitExceeded

logger = logging.getLogger(__name__)
MAX_LOGO_BYTES = 1_048_576
EXT_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
LOGO_EXTENSIONS = set(EXT_FORMATS)
_IMAGE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    ValueError,
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
)


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
        ext = os.path.splitext(logo.name)[1].lower()
        if ext not in LOGO_EXTENSIONS:
            raise forms.ValidationError("Logo must be a .png, .jpg, .jpeg or .webp file.")
        if logo.size > MAX_LOGO_BYTES:
            raise forms.ValidationError("Logo must be 1 MB or smaller.")
        logo.seek(0)
        raw = logo.read()
        logo.seek(0)
        expected = EXT_FORMATS[ext]
        try:
            Image.open(io.BytesIO(raw)).verify()
            img = Image.open(io.BytesIO(raw))
            if img.format != expected:
                raise forms.ValidationError("The file content does not match its extension.")
            img.load()
            clean = io.BytesIO()
            options = {"quality": 90} if expected in ("JPEG", "WEBP") else {}
            # Re-encode from pixels only: drops metadata and any trailing/embedded payload.
            fresh = img.copy()
            fresh.info = {k: v for k, v in img.info.items() if k == "transparency"}
            fresh.save(clean, expected, **options)
        except forms.ValidationError:
            raise
        except _IMAGE_ERRORS:
            raise forms.ValidationError("That file is not a valid image.") from None
        data = clean.getvalue()
        if len(data) > MAX_LOGO_BYTES:
            raise forms.ValidationError("Logo must be 1 MB or smaller.")
        return SimpleUploadedFile(logo.name, data, content_type=logo.content_type)

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
