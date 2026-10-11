"""handler400/403/404: §6.5 JSON for /api/ paths, Django's default pages elsewhere."""

import json

import pytest
from django.core.exceptions import BadRequest, PermissionDenied
from django.http import Http404
from django.test import Client, RequestFactory

from apps.core import views


@pytest.mark.django_db
def test_unknown_api_route_on_school_host_is_json_404(client_a):
    response = client_a.get("/api/v1/nope", headers={"X-Request-ID": "rid_404"})
    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Not found.",
            "details": [],
            "request_id": "rid_404",
        }
    }
    assert response["X-Request-ID"] == "rid_404"


@pytest.mark.django_db
def test_unknown_page_on_public_host_is_not_json(public_tenant):
    response = Client(HTTP_HOST="localhost").get("/nope")
    assert response.status_code == 404
    assert not response["Content-Type"].startswith("application/json")


@pytest.mark.django_db
def test_unknown_page_on_school_host_is_not_json(client_a):
    response = client_a.get("/nope")
    assert response.status_code == 404
    assert not response["Content-Type"].startswith("application/json")


@pytest.mark.parametrize(
    ("handler", "exception", "status", "code"),
    [
        (views.not_found, Http404(), 404, "NOT_FOUND"),
        (views.permission_denied, PermissionDenied(), 403, "FORBIDDEN"),
        (views.bad_request, BadRequest(), 400, "BAD_REQUEST"),
    ],
)
def test_handlers_return_json_for_api_and_html_elsewhere(handler, exception, status, code):
    api = handler(RequestFactory().get("/api/v1/x"), exception)
    assert api.status_code == status
    assert api["Content-Type"] == "application/json"
    error = json.loads(api.content)["error"]
    assert error["code"] == code
    assert error["details"] == []

    page = handler(RequestFactory().get("/x"), exception)
    assert page.status_code == status
    assert page["Content-Type"].startswith("text/html")


@pytest.mark.parametrize("urlconf", ["config.urls_tenant", "config.urls_public"])
def test_both_urlconfs_declare_the_handlers(urlconf):
    import importlib

    module = importlib.import_module(urlconf)
    assert module.handler400 == "apps.core.views.bad_request"
    assert module.handler403 == "apps.core.views.permission_denied"
    assert module.handler404 == "apps.core.views.not_found"
    assert module.handler500 == "apps.core.views.server_error"
