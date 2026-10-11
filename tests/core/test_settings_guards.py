import os
import subprocess
import sys

from django.conf import settings

_SNIPPET = "import django; django.setup(); from django.conf import settings; print(settings.DEBUG)"


def _import_settings(**env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", **env_overrides}
    return subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_production_without_redis_refuses_to_start():
    """DEBUG off with no REDIS_URL would silently run Celery tasks inline (eager mode)."""
    result = _import_settings(DEBUG="false", REDIS_URL="")
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and "REDIS_URL" in result.stderr


def test_production_with_redis_starts():
    result = _import_settings(DEBUG="false", REDIS_URL="redis://localhost:6379/0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_debug_without_redis_starts():
    result = _import_settings(DEBUG="true", REDIS_URL="")
    assert result.returncode == 0, result.stderr
