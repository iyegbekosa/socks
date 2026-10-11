import pytest

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.core.urls_testing")]


def test_api_error_body(client_a):
    response = client_a.get("/api-error")
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "SCORE_EXCEEDS_MAX",
            "message": "CA1 score cannot be more than 10",
            "details": [{"student": "GFS/2026/0042", "field": "ca1"}],
            "request_id": response["X-Request-ID"],
        }
    }


def test_permission_denied(client_a):
    response = client_a.get("/forbidden")
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "FORBIDDEN"
    assert body["details"] == []
    assert body["request_id"] == response["X-Request-ID"]


def test_not_found(client_a):
    response = client_a.get("/not-found")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_throttled(client_a):
    response = client_a.get("/throttled")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response["Retry-After"] == "7"


def test_validation_error(client_a):
    response = client_a.get("/validation")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == [{"field": "ca1", "message": "Too big."}]


def test_not_authenticated_maps_to_401():
    from rest_framework.exceptions import NotAuthenticated

    from apps.core.errors import api_exception_handler

    response = api_exception_handler(NotAuthenticated(), {})
    assert response.status_code == 401
    assert response.data["error"]["code"] == "NOT_AUTHENTICATED"
