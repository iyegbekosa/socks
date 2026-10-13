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


PAYSTACK_GATEWAY = "apps.fees.gateways.paystack.PaystackGateway"


@register(Tags.security, deploy=True)
def check_paystack_secret_key(app_configs: Any, **kwargs: Any) -> list[Error]:
    """With the Paystack gateway and no secret key, the payout page, Re-check and every webhook
    fail with ImproperlyConfigured at request time; catch it at deploy instead (M5)."""
    if settings.DEBUG:
        return []
    if settings.PAYMENT_GATEWAY == PAYSTACK_GATEWAY and not settings.PAYSTACK_SECRET_KEY:
        return [
            Error(
                "PAYMENT_GATEWAY is Paystack but PAYSTACK_SECRET_KEY is empty.",
                hint="Set PAYSTACK_SECRET_KEY (sk_live_… or sk_test_…).",
                id="core.E002",
            )
        ]
    return []
