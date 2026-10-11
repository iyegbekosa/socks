import cloudinary
import pytest
from django.apps import apps
from django.test import override_settings


@pytest.fixture(autouse=True)
def _restore_cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    yield
    current = cloudinary.config().__dict__
    current.clear()
    current.update(old)


@override_settings(CLOUDINARY_URL="cloudinary://key123:secret456@my-cloud")
def test_ready_applies_cloudinary_url():
    apps.get_app_config("core").ready()
    config = cloudinary.config()
    assert config.cloud_name == "my-cloud"
    assert config.api_key == "key123"
    assert config.api_secret == "secret456"


@override_settings(CLOUDINARY_URL="")
def test_ready_tolerates_empty_cloudinary_url():
    cloudinary.config(cloud_name="unchanged")
    apps.get_app_config("core").ready()
    assert cloudinary.config().cloud_name == "unchanged"
