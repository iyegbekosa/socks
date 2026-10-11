import json

import pytest
from django_tenants.utils import schema_context

from tests.isolation.registry import (
    EXEMPT_ROUTES,
    ISOLATION_CASES,
    unregistered_api_routes,
)


def _snapshot(obj):
    obj.refresh_from_db()
    return {f.attname: getattr(obj, f.attname) for f in obj._meta.concrete_fields}


def _request_kwargs(case):
    if case.method.upper() in {"POST", "PUT", "PATCH"}:
        return {"data": json.dumps(case.body or {}), "content_type": "application/json"}
    return {}


@pytest.mark.django_db
@pytest.mark.parametrize("case", ISOLATION_CASES, ids=lambda c: f"{c.name} {c.method.upper()}")
def test_cross_school_access_is_refused(case, school_b, client_a):
    with schema_context(school_b.schema_name):
        obj = case.make_obj()
        before = _snapshot(obj)

    response = getattr(client_a, case.method.lower())(case.url(obj), **_request_kwargs(case))

    assert response.status_code in {401, 403, 404}
    with schema_context(school_b.schema_name):
        assert _snapshot(obj) == before


def test_every_tenant_api_route_is_registered():
    missing = unregistered_api_routes("config.urls_tenant")
    assert not missing, (
        "Tenant API routes without a cross-school isolation case "
        f"(add to ISOLATION_CASES or EXEMPT_ROUTES): {missing}"
    )


def test_guard_reports_unregistered_and_unnamed_routes():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={("registered-route", "GET")}
    )
    assert "student-list GET" in missing
    assert "registered-route GET" not in missing
    assert not any(m.startswith("healthz") for m in missing)
    assert not any(m.startswith("not-under-api") for m in missing)
    assert any("api/v1/anonymous/" in m for m in missing)


def test_guard_reports_re_path_routes():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={("registered-route", "GET")}
    )
    assert "regex-route GET" in missing
    assert "nested-regex GET" in missing


def test_exempt_routes_exist():
    from django.urls import NoReverseMatch, reverse

    for name in EXEMPT_ROUTES:
        try:
            reverse(name, urlconf="config.urls_tenant")
        except NoReverseMatch:
            pytest.fail(f"exempt route {name!r} does not resolve in config.urls_tenant")


def test_guard_is_method_aware():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={("things:detail", "GET")}
    )
    assert "things:detail PATCH" in missing
    assert "things:detail GET" not in missing


def test_guard_method_comparison_is_case_insensitive_and_exempt_covers_all_methods():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered",
        registered={("things:detail", "get"), ("things:detail", "patch")},
    )
    assert not any(m.startswith("things:detail") for m in missing)


def test_default_registered_set_is_built_from_cases(monkeypatch):
    from tests.isolation import registry

    case = registry.IsolationCase(
        name="things:detail", method="patch", url=lambda o: "", make_obj=lambda: None
    )
    monkeypatch.setattr(registry, "ISOLATION_CASES", [case])
    missing = registry.unregistered_api_routes("tests.isolation.urlconf_unregistered")
    assert "things:detail PATCH" not in missing
    assert "things:detail GET" in missing


def test_guard_distinguishes_namespaces_sharing_a_leaf_name():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={("students:detail", "GET")}
    )
    assert "teachers:detail GET" in missing
    assert "students:detail GET" not in missing


def test_namespaced_case_name_satisfies_guard():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={("students:list", "GET")}
    )
    assert "students:list GET" not in missing
    assert "students:detail GET" in missing
