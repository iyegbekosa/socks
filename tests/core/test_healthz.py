from unittest import mock

import pytest


@pytest.mark.django_db
def test_healthz_reports_db_and_cache_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "cache": "ok"}


@pytest.mark.django_db
def test_healthz_returns_503_when_cache_down(client):
    with mock.patch("django.core.cache.cache.set", side_effect=RuntimeError("cache down")):
        response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["cache"] == "error"
    assert body["db"] == "ok"
    assert body["status"] == "error"


@pytest.mark.django_db
def test_healthz_on_school_host(client_a):
    response = client_a.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
