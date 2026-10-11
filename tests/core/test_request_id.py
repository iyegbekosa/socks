import re

import pytest

from apps.core.request_id import get_request_id

GENERATED = re.compile(r"^req_[0-9a-f]{12}$")
pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.core.urls_testing")]


def test_valid_incoming_id_is_echoed(client_a):
    response = client_a.get("/log", headers={"X-Request-ID": "abc_123"})
    assert response["X-Request-ID"] == "abc_123"


def test_missing_id_is_generated(client_a):
    response = client_a.get("/log")
    assert GENERATED.match(response["X-Request-ID"])


@pytest.mark.parametrize(
    "bad",
    ["evil\nheader", "x" * 10_000, "has space", "a" * 65],
    ids=["newline", "10k", "space", "65-chars"],
)
def test_hostile_id_is_replaced(client_a, bad):
    response = client_a.get("/log", headers={"X-Request-ID": bad})
    assert GENERATED.match(response["X-Request-ID"])


def test_hostile_id_with_newline_via_wsgi_environ(client_a):
    response = client_a.get("/log", HTTP_X_REQUEST_ID="evil\nheader")
    assert GENERATED.match(response["X-Request-ID"])


def test_context_reset_after_request(client_a):
    client_a.get("/log", headers={"X-Request-ID": "abc_123"})
    assert get_request_id() == "-"


def test_context_reset_when_view_raises(client_a):
    client_a.raise_request_exception = False
    client_a.get("/boom", headers={"X-Request-ID": "abc_123"})
    assert get_request_id() == "-"
