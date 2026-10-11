import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context, tenant_context

from apps.academics.models import AcademicSettings, AdmissionCounter
from apps.academics.services import (
    default_prefix,
    get_academic_settings,
    next_admission_number,
    preview_next_admission_number,
    render_admission_number,
    update_admission_settings,
    validate_admission_pattern,
    validate_manual_admission_number,
)
from apps.accounts.identifiers import looks_like_phone
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean(school_a):
    with schema_context("school_a"):
        AcademicSettings.objects.all().delete()
        AdmissionCounter.objects.all().delete()
    yield
    with schema_context("school_a"):
        AcademicSettings.objects.all().delete()
        AdmissionCounter.objects.all().delete()


@pytest.fixture
def admin_user(school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["school_admin"], by=u)
    return u


def configure(prefix="GFS", pattern="{PREFIX}/{YEAR}/{SEQ:4}", by=None):
    return update_admission_settings(prefix=prefix, pattern=pattern, by=by)


def test_looks_like_phone():
    assert looks_like_phone("08031234567")
    assert looks_like_phone("0803 123-4567")
    assert looks_like_phone("+2348031234567")
    assert not looks_like_phone("GFS/2026/0001")
    assert not looks_like_phone("2026000001")
    assert not looks_like_phone("")


def test_default_prefix_from_school_name(school_a):
    with tenant_context(school_a):
        assert default_prefix() == "SA"
        assert get_academic_settings().admission_prefix == "SA"


def test_default_prefix_fallbacks(school_a, monkeypatch):
    with tenant_context(school_a):
        monkeypatch.setattr(connection.tenant, "name", "123 456", raising=False)
        assert default_prefix() == "STU"
        monkeypatch.setattr(
            connection.tenant,
            "name",
            "Great Foundation Model Academy Of Lagos State Ng",
            raising=False,
        )
        assert default_prefix() == "GFMAOLSN"
        monkeypatch.setattr(
            connection.tenant,
            "name",
            "Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota",
            raising=False,
        )
        assert len(default_prefix()) == 8


def test_sequence_and_year_restart(school_a):
    with schema_context("school_a"), transaction.atomic():
        configure()
        assert [next_admission_number(2026) for _ in range(3)] == [
            "GFS/2026/0001",
            "GFS/2026/0002",
            "GFS/2026/0003",
        ]
        assert next_admission_number(2027) == "GFS/2027/0001"


def test_requires_atomic_block(school_a):
    # django_db wraps the test in an atomic block, so patch the flag to emulate autocommit.
    from unittest import mock

    with schema_context("school_a"):
        with mock.patch.object(type(connection), "in_atomic_block", False, create=True):
            with pytest.raises(RuntimeError):
                next_admission_number(2026)


def test_skips_numbers_already_used(school_a):
    with schema_context("school_a"), transaction.atomic():
        configure()
        StudentFactory(admission_number="GFS/2026/0001")
        StudentFactory(admission_number="GFS/2026/0002")
        assert next_admission_number(2026) == "GFS/2026/0003"
        assert AdmissionCounter.objects.get(year=2026).last == 3


def test_preview_does_not_increment(school_a):
    with schema_context("school_a"):
        configure()
        assert preview_next_admission_number(2026) == "GFS/2026/0001"
        assert preview_next_admission_number(2026) == "GFS/2026/0001"
        assert not AdmissionCounter.objects.exists()


def test_year_seq_pattern_allowed(school_a):
    with schema_context("school_a"), transaction.atomic():
        configure(pattern="{YEAR}{SEQ:6}")
        assert next_admission_number(2026) == "2026000001"


@pytest.mark.parametrize(
    "pattern",
    [
        "08031{SEQ:6}",
        "{PREFIX}/{YEAR}",
        "{SEQ:4}/{SEQ:4}",
        "{SEQ:2}",
        "{SEQ:7}",
        "{PREFIX}/{foo}{SEQ:4}",
        "ab{SEQ:4}!",
        "0{SEQ:3}{YEAR}000",
        "070{SEQ:3}{YEAR}",
        "0803-12-{SEQ:4}",
        "{SEQ:6}{YEAR}",
        "1{SEQ:6}",
        "234{SEQ:6}",
    ],
)
def test_bad_patterns_rejected(pattern):
    with pytest.raises(ValidationError):
        validate_admission_pattern(pattern.upper(), "GFS")


@pytest.mark.parametrize(
    "pattern", ["{YEAR}{SEQ:6}", "GFS/{YEAR}/{SEQ:4}", "{PREFIX}{SEQ:5}", "{YEAR}-{SEQ:4}"]
)
def test_good_patterns_never_phone_shaped(pattern):
    validate_admission_pattern(pattern, "GFS")
    seqs = [*range(1, 10**6, 997), 9, 10, 99, 100, 999, 1000, 9999, 10000, 99999, 100000, 999999]
    for year in range(2020, 2041):
        for seq in seqs:
            assert not looks_like_phone(render_admission_number(pattern, "GFS", year, seq))


def test_default_prefix_under_bare_schema_context(school_a):
    with schema_context("school_a"):
        assert get_academic_settings().admission_prefix == "SA"


def test_overlong_pattern_rejected():
    with pytest.raises(ValidationError):
        validate_admission_pattern("A" * 30 + "{SEQ:4}", "GFS")


def test_bad_prefix_rejected(school_a):
    with schema_context("school_a"):
        for bad in ["", "gfs1", "ABCDEFGHI", "G-S"]:
            with pytest.raises(ValidationError):
                configure(prefix=bad)


def test_runtime_phone_shaped_result_refused(school_a):
    # A legacy pattern saved without validation; the runtime guard must still refuse it.
    with schema_context("school_a"), transaction.atomic():
        AcademicSettings.objects.create(
            pk=1, admission_prefix="GFS", admission_pattern="0803{SEQ:3}"
        )
        AdmissionCounter.objects.create(year=2026, last=1_234_566)
        with pytest.raises(ValidationError):
            next_admission_number(2026)  # would render 08031234567
        assert AdmissionCounter.objects.get(year=2026).last == 1_234_566


def test_runtime_overflow_too_long(school_a):
    with schema_context("school_a"), transaction.atomic():
        configure(prefix="ABCDEFGH", pattern="{PREFIX}/{PREFIX}/{PREFIX}/{SEQ:5}")
        AdmissionCounter.objects.create(year=2026, last=99_999)
        with pytest.raises(ValidationError):
            next_admission_number(2026)


def test_manual_numbers(school_a):
    with schema_context("school_a"):
        StudentFactory(admission_number="GFS/2026/0001")
        for bad in ["08031234567", "gfs/2026/0001", "", "A" * 33, "AB CD", "AB_1"]:
            with pytest.raises(ValidationError):
                validate_manual_admission_number(bad)
        assert validate_manual_admission_number(" gfs/2026/0099 ") == "GFS/2026/0099"


def test_settings_page_admin_edit_and_audit(client_a, admin_user):
    login_verified(client_a, admin_user)
    url = reverse("academics:settings")
    resp = client_a.get(url)
    assert resp.status_code == 200
    year = timezone.localdate().year
    assert f"SA/{year}/0001".encode() in resp.content
    with schema_context("school_a"):
        assert not AcademicSettings.objects.exists()  # GET must not create the row
    resp = client_a.post(url, {"admission_prefix": "gfs", "admission_pattern": "{PREFIX}-{SEQ:5}"})
    assert resp.status_code == 302
    with schema_context("school_a"):
        conf = AcademicSettings.objects.get(pk=1)
        # prefix is uppercased by the service
        assert (conf.admission_prefix, conf.admission_pattern) == ("GFS", "{PREFIX}-{SEQ:5}")
        ev = AuditEvent.objects.get(action="academics.admission_pattern_changed")
        assert ev.after["prefix"] == "GFS"
        assert ev.before["pattern"] == "{PREFIX}/{YEAR}/{SEQ:4}"
        assert not AdmissionCounter.objects.exists()


def test_settings_page_rejects_phone_pattern(client_a, admin_user):
    login_verified(client_a, admin_user)
    resp = client_a.post(
        reverse("academics:settings"),
        {"admission_prefix": "GFS", "admission_pattern": "08031{SEQ:6}"},
    )
    assert resp.status_code == 200
    assert b"look like phone" in resp.content


@pytest.mark.parametrize("role", ["proprietor", "principal"])
def test_settings_read_only_roles(client_a, school_a, role):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, [role], by=u)
    login_verified(client_a, u)
    url = reverse("academics:settings")
    assert client_a.get(url).status_code == 200
    resp = client_a.post(url, {"admission_prefix": "XX", "admission_pattern": "{SEQ:4}"})
    assert resp.status_code == 403
    with schema_context("school_a"):
        assert not AuditEvent.objects.filter(action="academics.admission_pattern_changed").exists()


def test_settings_forbidden_for_teacher(client_a, school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher"], by=u)
    with schema_context("school_a"):
        client_a.force_login(u)
    assert client_a.get(reverse("academics:settings")).status_code == 403
