from datetime import date

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.attendance.models import AttendanceRecord, AttendanceSettings, Holiday
from apps.audit.models import AuditEvent
from tests.accounts.helpers import login_verified
from tests.fees.factories import make_staff, make_terms
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def _user(role):
    with schema_context("school_a"):
        return make_staff(role)


def _login(client, user, *, mfa):
    if mfa:
        login_verified(client, user)
    else:
        with schema_context("school_a"):
            client.force_login(user)


def _audit(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action))


def test_urls():
    assert reverse("attendance:settings") == "/attendance/settings/"
    assert reverse("attendance:holidays") == "/attendance/holidays/"


def test_defaults(school_a):
    with schema_context("school_a"):
        s = AttendanceSettings.load()
        assert (s.late_threshold, s.edit_window_days, s.absence_email) == (3, 7, True)


def test_admin_saves_settings_and_is_audited(client_a):
    admin = _user("school_admin")
    login_verified(client_a, admin)
    r = client_a.post(
        "/attendance/settings/",
        {"late_threshold": "5", "edit_window_days": "7", "absence_email": ""},
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        s = AttendanceSettings.load()
        assert (s.late_threshold, s.edit_window_days, s.absence_email) == (5, 7, False)
    (ev,) = _audit("attendance.settings_saved")
    assert ev.before == {"late_threshold": 3, "absence_email": True}
    assert ev.after == {"late_threshold": 5, "absence_email": False}


def test_unchanged_settings_write_no_audit(client_a):
    login_verified(client_a, _user("school_admin"))
    client_a.post(
        "/attendance/settings/",
        {"late_threshold": "3", "edit_window_days": "7", "absence_email": "on"},
    )
    assert _audit("attendance.settings_saved") == []


def test_settings_validation_range(client_a):
    login_verified(client_a, _user("school_admin"))
    r = client_a.post(
        "/attendance/settings/",
        {"late_threshold": "61", "edit_window_days": "-1", "absence_email": "on"},
    )
    assert r.status_code == 200
    assert _audit("attendance.settings_saved") == []


def test_admin_adds_holiday_and_is_audited(client_a):
    login_verified(client_a, _user("school_admin"))
    r = client_a.post("/attendance/holidays/", {"date": "2026-01-07", "name": "Founders day"})
    assert r.status_code == 302
    with schema_context("school_a"):
        h = Holiday.objects.get(date=date(2026, 1, 7))
    (ev,) = _audit("attendance.holiday_saved")
    assert ev.after == {"holiday_id": h.pk, "date": "2026-01-07"}


def test_duplicate_holiday_is_form_error(client_a):
    login_verified(client_a, _user("school_admin"))
    with schema_context("school_a"):
        Holiday.objects.create(date=date(2026, 1, 7), name="x")
    r = client_a.post("/attendance/holidays/", {"date": "2026-01-07", "name": "y"})
    assert r.status_code == 200
    assert _audit("attendance.holiday_saved") == []


def test_holiday_conflicts_with_existing_register(client_a):
    admin = _user("school_admin")
    login_verified(client_a, admin)
    with schema_context("school_a"):
        w = make_terms()
        stu = StudentProfileFactory()
        AttendanceRecord.objects.create(
            student=stu,
            date=date(2026, 1, 6),
            arm=w.level.arms.first(),
            status="present",
            recorded_by=admin,
        )
    r = client_a.post("/attendance/holidays/", {"date": "2026-01-06", "name": "Oops"})
    assert r.status_code == 200
    assert "A register was already taken on this date." in r.content.decode()
    with schema_context("school_a"):
        assert not Holiday.objects.filter(date=date(2026, 1, 6)).exists()


@pytest.mark.parametrize("role", ["principal", "proprietor"])
def test_principal_and_proprietor_read_only(client_a, role):
    user = _user(role)
    _login(client_a, user, mfa=role == "principal")
    for url in ("/attendance/settings/", "/attendance/holidays/"):
        assert client_a.get(url).status_code == 200
    assert client_a.post("/attendance/settings/", {"late_threshold": "9"}).status_code == 403
    assert (
        client_a.post("/attendance/holidays/", {"date": "2026-01-07", "name": "x"}).status_code
        == 403
    )


@pytest.mark.parametrize("role", ["teacher", "bursar"])
def test_other_roles_get_403(client_a, role):
    user = _user(role)
    _login(client_a, user, mfa=role == "bursar")
    assert client_a.get("/attendance/settings/").status_code == 403
    assert client_a.get("/attendance/holidays/").status_code == 403


def test_home_link_for_admin(client_a):
    login_verified(client_a, _user("school_admin"))
    assert "Attendance settings" in client_a.get("/").content.decode()
