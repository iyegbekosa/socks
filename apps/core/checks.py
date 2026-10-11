"""Deploy-time system checks (``manage.py check --deploy``)."""

from typing import Any

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def check_platform_url_template(app_configs: Any, **kwargs: Any) -> list[Error]:
    """Invite and portal links are built from PLATFORM_URL_TEMPLATE; in production they must
    point at the real domain over https."""
    if settings.DEBUG:
        return []
    template = settings.PLATFORM_URL_TEMPLATE
    if "localhost" in template or not template.startswith("https://"):
        return [
            Error(
                f"PLATFORM_URL_TEMPLATE is {template!r}; in production it must start with "
                "https:// and use the real platform domain.",
                hint="e.g. PLATFORM_URL_TEMPLATE=https://{subdomain}.build2learn.ng",
                id="core.E001",
            )
        ]
    return []
