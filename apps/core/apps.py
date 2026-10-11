from django.apps import AppConfig
from django.conf import settings


def configure_cloudinary(url: str) -> None:
    """Apply ``settings.CLOUDINARY_URL`` to the Cloudinary SDK; no-op when empty.

    ``cloudinary.config(**kwargs)`` stores keys verbatim and has no ``cloudinary_url``
    option that gets parsed, so the SDK's own URL parser is used (it sets cloud_name,
    api_key, api_secret, private CDN and query-string options, and rejects bad schemes).
    """
    if not url:
        return
    import cloudinary

    cloudinary.config()._load_from_url(url)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        from apps.core import checks  # noqa: F401  (registers the deploy checks)

        configure_cloudinary(settings.CLOUDINARY_URL)
