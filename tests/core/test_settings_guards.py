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


def test_sentry_init_excludes_local_variables():
    """Frame locals can hold children's data (DOB, guardian contacts): never send them."""
    snippet = (
        "import sentry_sdk; calls = []; "
        "sentry_sdk.init = lambda **kw: calls.append(kw); "
        "import django; django.setup(); "
        "print(len(calls), calls[0]['include_local_variables'], calls[0]['send_default_pii'], "
        "calls[0]['max_request_body_size'])"
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "SENTRY_DSN": "https://key@sentry.example/1",
        "DEBUG": "true",
        "REDIS_URL": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1 False False never"


def test_sentry_before_send_drops_request_body_query_and_cookies():
    """Request bodies carry children's data and passwords; never let them reach Sentry."""
    from config.settings import _sentry_before_send

    event = {
        "request": {
            "url": "https://school-a.localhost/students/",
            "method": "POST",
            "data": {
                "date_of_birth": "01/02/2015",
                "guardian_phone": "+2348031234567",
                "new_password1": "Sup3rSecret!pw",
            },
            "query_string": "q=Adaeze+Okafor",
            "cookies": {"sessionid": "abc123sessioncookie"},
        }
    }
    out = _sentry_before_send(event, None)
    dumped = repr(out)
    for value in (
        "01/02/2015",
        "+2348031234567",
        "Sup3rSecret!pw",
        "Adaeze",
        "abc123sessioncookie",
    ):
        assert value not in dumped
    assert "data" not in out["request"]
    assert "query_string" not in out["request"]
    assert "cookies" not in out["request"]
    assert out["request"]["url"] == "https://school-a.localhost/students/"
