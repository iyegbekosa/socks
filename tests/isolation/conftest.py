import pytest

from tests.imports.conftest import _cloudinary_config, cloud  # noqa: F401


@pytest.fixture(autouse=True)
def _hermetic_uploads(cloud, _cloudinary_config):  # noqa: F811
    """Isolation cases that upload files must never reach the real Cloudinary."""
