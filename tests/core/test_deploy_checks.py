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
