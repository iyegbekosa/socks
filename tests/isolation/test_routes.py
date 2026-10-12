import pytest

from tests.isolation.registry import (
    EXEMPT_ROUTES,
    ISOLATION_CASES,
    IsolationCase,
    assert_cross_school_refused,
    unregistered_api_routes,
)


@pytest.mark.django_db
@pytest.mark.parametrize("case", ISOLATION_CASES, ids=lambda c: c.test_id)
def test_cross_school_access_is_refused(case, school_a, school_b, client_a):
    assert_cross_school_refused(case, school_a, school_b, client_a)


def test_every_tenant_api_route_is_registered():
    missing = unregistered_api_routes("config.urls_tenant")
    assert not missing, (
        "Tenant API routes without a cross-school isolation case "
        f"(add to ISOLATION_CASES or EXEMPT_ROUTES): {missing}"
    )


def test_tenant_api_has_routes():
    """Sentinel: an empty api/v1/ tree would make the guard vacuous."""
    from django.urls import get_resolver

    from tests.isolation.registry import API_PREFIX, _walk

    routes = [r for r, _n, _m in _walk(get_resolver("config.urls_tenant").url_patterns)]
    assert any(r.startswith(API_PREFIX) for r in routes)
    assert ("api:me", "GET") in {(c.name, c.method) for c in ISOLATION_CASES}


def test_case_ids_are_unique_and_me_has_both_cases():
    ids = [c.test_id for c in ISOLATION_CASES]
    assert len(ids) == len(set(ids))
    me = {c.label: c for c in ISOLATION_CASES if c.name == "api:me"}
    assert me["anonymous"].actor is None
    assert me["authenticated"].actor is not None and me["authenticated"].expect == "absent"


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


def test_guard_reads_viewset_actions_mapping():
    missing = unregistered_api_routes(
        "tests.isolation.urlconf_unregistered", registered={("widget-detail", "GET")}
    )
    assert "widget-detail PATCH" in missing
    assert "widget-detail GET" not in missing


def test_guard_reads_plain_class_based_view_handlers():
    missing = unregistered_api_routes("tests.isolation.urlconf_unregistered")
    assert "gadget-list GET" in missing
    assert "gadget-list POST" in missing
    assert not any(
        m.startswith("gadget-list") and m.split()[1] in {"HEAD", "OPTIONS"} for m in missing
    )


# --- the shared check itself, driven through test-only leaky/well-behaved views ---


@pytest.fixture
def leaky_urls(settings):
    settings.ROOT_URLCONF = "tests.isolation.urlconf_leaky"


def _student_in_school_b():
    from tests.accounts.factories import StudentFactory

    return StudentFactory.create()


def _case(name, url, **kw):
    return IsolationCase(
        name, "GET", url=lambda o: url.format(pk=o.pk), make_obj=_student_in_school_b, **kw
    )


def _marker(obj):
    return obj.admission_number


def test_absent_case_requires_marker():
    with pytest.raises(ValueError):
        _case("x", "/x/", expect="absent")


@pytest.mark.django_db
def test_leaky_detail_is_caught(leaky_urls, school_a, school_b, client_a):
    case = _case("leaky:detail", "/api/v1/leaky/{pk}/")
    with pytest.raises(AssertionError):
        assert_cross_school_refused(case, school_a, school_b, client_a)


@pytest.mark.django_db
def test_safe_detail_passes(leaky_urls, school_a, school_b, client_a):
    case = _case("safe:detail", "/api/v1/safe/{pk}/")
    assert_cross_school_refused(case, school_a, school_b, client_a)


@pytest.mark.django_db
def test_leaky_list_is_caught(leaky_urls, school_a, school_b, client_a):
    case = _case("leaky:list", "/api/v1/leaky/", expect="absent", marker=_marker)
    with pytest.raises(AssertionError):
        assert_cross_school_refused(case, school_a, school_b, client_a)


@pytest.mark.django_db
def test_safe_list_passes(leaky_urls, school_a, school_b, client_a):
    case = _case("safe:list", "/api/v1/safe/", expect="absent", marker=_marker)
    assert_cross_school_refused(case, school_a, school_b, client_a)


@pytest.mark.django_db
def test_actor_is_really_logged_in(leaky_urls, school_a, school_b, client_a):
    """The leaky views need a session: an anonymous case against them is refused (not a leak)."""
    case = _case("leaky:detail", "/api/v1/leaky/{pk}/", actor=None)
    assert_cross_school_refused(case, school_a, school_b, client_a)


def _reg(name):
    return next(c for c in ISOLATION_CASES if c.name == name)


@pytest.mark.django_db
def test_import_case_fails_when_the_job_is_created_in_the_wrong_school(
    school_a, school_b, client_a, monkeypatch
):
    """Harness self-test: a leaky import view must not pass the no_cross_effect case."""
    from django_tenants.utils import schema_context

    from apps.imports.models import ImportJob
    from tests.accounts.factories import StaffFactory

    def leaky(*, uploaded_file, kind, user, request):
        with schema_context("school_b"):
            return ImportJob.objects.create(
                kind=kind, file_name="x", created_by=StaffFactory.create()
            )

    monkeypatch.setattr("apps.api.views.create_import_job", leaky)
    with pytest.raises(AssertionError):
        assert_cross_school_refused(_reg("api:students_import"), school_a, school_b, client_a)


@pytest.mark.django_db
def test_import_case_passes_for_the_real_view(school_a, school_b, client_a):
    assert_cross_school_refused(_reg("api:students_import"), school_a, school_b, client_a)
