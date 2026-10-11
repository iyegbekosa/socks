from django.core.cache import cache
from django.core.files.storage import storages
from django.core.validators import RegexValidator
from django.db import models, transaction

from apps.core.storage import unique_upload_name

CACHE_KEY = "branding"
LOGO_WIDTH = 240
COLOR_VALIDATOR = RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Enter a colour like #1F4E79.")


def public_storage():
    return storages["public"]


def logo_upload_to(instance, filename):
    return unique_upload_name("branding", filename)


class Branding(models.Model):
    """Per-school branding singleton (pk is always 1)."""

    motto = models.CharField(max_length=200, blank=True)
    address = models.CharField(max_length=300, blank=True)
    primary_color = models.CharField(max_length=7, default="#1F4E79", validators=[COLOR_VALIDATOR])
    accent_color = models.CharField(max_length=7, default="#F2B705", validators=[COLOR_VALIDATOR])
    logo = models.ImageField(
        upload_to=logo_upload_to, storage=public_storage, blank=True, max_length=255
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "School branding"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(CACHE_KEY)
        transaction.on_commit(lambda: cache.delete(CACHE_KEY))

    @property
    def logo_url(self) -> str | None:
        if not self.logo:
            return None
        return public_storage().image_url(self.logo.name, width=LOGO_WIDTH)


def get_branding() -> Branding:
    branding = cache.get(CACHE_KEY)
    if branding is None:
        branding, _ = Branding.objects.get_or_create(pk=1)
        cache.set(CACHE_KEY, branding, 300)
    return branding
