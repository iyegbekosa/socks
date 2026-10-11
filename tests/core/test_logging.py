import json
import logging

import pytest

from apps.core.logging import ContextFilter, JsonFormatter, RedactFilter


@pytest.mark.django_db
@pytest.mark.urls("tests.core.urls_testing")
def test_log_line_in_request_has_schema_and_request_id(client_a, caplog):
    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.WARNING, logger="tests.core"):
        response = client_a.get("/log", headers={"X-Request-ID": "rid_1"})
    records = [r for r in caplog.records if r.name == "tests.core"]
    assert records
    assert records[0].schema == "school_a"
    assert records[0].request_id == response["X-Request-ID"] == "rid_1"
    assert records[0].user_id == "-"
    assert records[0].role == "-"


def test_context_defaults_outside_request():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    ContextFilter().filter(record)
    assert (record.request_id, record.schema, record.user_id, record.role) == ("-",) * 4


def test_redaction_in_json_output():
    logger = logging.getLogger("tests.redact")
    record = logger.makeRecord(
        "tests.redact",
        logging.INFO,
        "f",
        1,
        "login",
        None,
        None,
        extra={"password": "x", "otp": "123456", "Auth_Token": "t", "username": "ada"},
    )
    ContextFilter().filter(record)
    RedactFilter().filter(record)
    out = json.loads(JsonFormatter().format(record))
    assert out["password"] == "[REDACTED]"
    assert out["otp"] == "[REDACTED]"
    assert out["Auth_Token"] == "[REDACTED]"
    assert out["username"] == "ada"
    assert out["request_id"] == "-"
    assert out["message"] == "login"


@pytest.mark.django_db
@pytest.mark.urls("tests.core.urls_testing")
def test_authenticated_request_log_has_user_id(client_a, school_a, caplog):
    from django.contrib.auth import get_user_model
    from django_tenants.utils import schema_context

    with schema_context(school_a.schema_name):
        user = get_user_model().objects.create_user(username="ada", password="pw-12345-x")
        client_a.force_login(user)
        caplog.handler.addFilter(ContextFilter())
        with caplog.at_level(logging.WARNING, logger="tests.core"):
            client_a.get("/log")
        pk = str(user.pk)
        user.delete()
    records = [r for r in caplog.records if r.name == "tests.core"]
    assert records
    assert records[0].user_id == pk
    assert records[0].role == "-"


@pytest.fixture
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.usefixtures("_restore_root_logging")
def test_logging_settings_wire_filters_and_json_formatter():
    import logging.config

    from django.conf import settings

    logging.config.dictConfig(settings.LOGGING)
    handlers = logging.getLogger().handlers
    assert handlers
    for handler in handlers:
        assert any(isinstance(f, ContextFilter) for f in handler.filters)
        assert any(isinstance(f, RedactFilter) for f in handler.filters)
        assert isinstance(handler.formatter, JsonFormatter)


def test_celery_does_not_hijack_root_logger():
    from config.celery import app

    assert app.conf.worker_hijack_root_logger is False


@pytest.mark.usefixtures("_restore_root_logging")
def test_celery_setup_logging_signal_applies_django_logging():
    from unittest import mock

    from celery.signals import setup_logging
    from django.conf import settings

    from config.celery import configure_logging

    with mock.patch("logging.config.dictConfig") as dict_config:
        responses = setup_logging.send(
            sender=None, loglevel="INFO", logfile=None, format="", colorize=False
        )
    assert configure_logging in [receiver for receiver, _ in responses]
    dict_config.assert_called_once_with(settings.LOGGING)

    # And for real: the root handler ends up with the JSON formatter.
    setup_logging.send(sender=None, loglevel="INFO", logfile=None, format="", colorize=False)
    assert any(isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers)


@pytest.mark.parametrize(
    "key",
    ["password", "new_password", "password1", "otp", "otp_code", "card_number", "user_pin",
     "api_token", "client_secret", "Authorization", "Auth-Token", "x.secret"],
)  # fmt: skip
def test_is_sensitive_key_true(key):
    from apps.core.logging import is_sensitive_key

    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key", ["shipping", "mapping", "footprint", "discard", "pinned", "tokenizer", "username"]
)
def test_is_sensitive_key_false(key):
    from apps.core.logging import is_sensitive_key

    assert not is_sensitive_key(key)


def test_redact_filter_keeps_innocent_keys():
    logger = logging.getLogger("tests.redact2")
    record = logger.makeRecord(
        "tests.redact2", logging.INFO, "f", 1, "m", None, None,
        extra={"shipping": "lagos", "api_token": "t"},
    )  # fmt: skip
    RedactFilter().filter(record)
    assert record.shipping == "lagos"
    assert record.api_token == "[REDACTED]"
