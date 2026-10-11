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
