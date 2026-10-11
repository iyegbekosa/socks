import pytest
from django.forms.models import model_to_dict
from django_tenants.utils import schema_context

from tests.isolation.registry import (
    EXEMPT_ROUTES,
    ISOLATION_CASES,
    unregistered_api_routes,
)


@pytest.mark.django_db
@pytest.mark.parametrize("case", ISOLATION_CASES, ids=lambda c: c.name)
def test_cross_school_access_is_refused(case, school_b, client_a):
    with schema_context(school_b.schema_name):
        obj = case.make_obj()
        before = model_to_dict(obj)

    response = getattr(client_a, case.method.lower())(
        case.url(obj), data=case.body, content_type="application/json"
    )

    assert response.status_code in {401, 403, 404}
    with schema_context(school_b.schema_name):
        assert model_to_dict(type(obj).objects.get(pk=obj.pk)) == before


def test_every_tenant_api_route_is_registered():
    missing = unregistered_api_routes("config.urls_tenant")
    assert not missing, (
        "Tenant API routes without a cross-school isolation case "
        f"(add to ISOLATION_CASES or EXEMPT_ROUTES): {missing}"
    )


def test_guard_reports_unregistered_and_unnamed_routes():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={"registered-route"}
    )
    assert "student-list" in missing
    assert "registered-route" not in missing
    assert "healthz" not in missing
    assert "not-under-api" not in missing
    assert any("api/v1/anonymous/" in m for m in missing)


def test_exempt_routes_start_with_healthz():
    assert "healthz" in EXEMPT_ROUTES
