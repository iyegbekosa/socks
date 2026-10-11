import os
import subprocess
import sys

from django.conf import settings


def test_signup_global_daily_limit_from_env():
    snippet = (
        "import django; django.setup(); from apps.tenancy.views import _global_limit; "
        "print(_global_limit.limit)"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings"}
    env["SIGNUP_GLOBAL_DAILY_LIMIT"] = "7"
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"
