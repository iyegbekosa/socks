import pytest
from django.core.checks import Tags, run_checks

CHECK_ID = "core.E001"


def _deploy_ids() -> set[str]:
    return {m.id for m in run_checks(tags=[Tags.security], include_deployment_checks=True)}


@pytest.mark.parametrize(
    "template",
    [
        "http://{subdomain}.localhost:8000",
        "https://{subdomain}.localhost",
        "http://{subdomain}.build2learn.ng",
    ],
)
def test_bad_platform_url_template_is_a_deploy_error(settings, template):
    settings.DEBUG = False
    settings.PLATFORM_URL_TEMPLATE = template
    assert CHECK_ID in _deploy_ids()


def test_https_platform_url_template_passes(settings):
    settings.DEBUG = False
    settings.PLATFORM_URL_TEMPLATE = "https://{subdomain}.build2learn.ng"
    assert CHECK_ID not in _deploy_ids()


def test_check_is_skipped_in_debug(settings):
    settings.DEBUG = True
    settings.PLATFORM_URL_TEMPLATE = "http://{subdomain}.localhost:8000"
    assert CHECK_ID not in _deploy_ids()


def test_check_is_deploy_only(settings):
    settings.DEBUG = False
    settings.PLATFORM_URL_TEMPLATE = "http://{subdomain}.localhost:8000"
    assert CHECK_ID not in {m.id for m in run_checks(tags=[Tags.security])}


PAYSTACK = "apps.fees.gateways.paystack.PaystackGateway"


@pytest.mark.parametrize(
    "debug, gateway, key, expected",
    [
        (False, PAYSTACK, "", True),
        (False, PAYSTACK, "sk_test_x", False),
        (False, "apps.fees.gateways.fake.FakeGateway", "", False),
        (True, PAYSTACK, "", False),
    ],
)
def test_paystack_without_secret_key_is_a_deploy_error(settings, debug, gateway, key, expected):
    settings.DEBUG = debug
    settings.PAYMENT_GATEWAY = gateway
    settings.PAYSTACK_SECRET_KEY = key
    assert ("core.E002" in _deploy_ids()) is expected
