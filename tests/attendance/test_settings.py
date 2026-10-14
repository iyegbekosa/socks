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


def _holiday(d=date(2026, 1, 7), name="Founders day"):
    with schema_context("school_a"):
        return Holiday.objects.create(date=d, name=name)


def _delete_url(h):
    return reverse("attendance:holiday_delete", args=[h.pk])


def test_admin_deletes_holiday_and_is_audited(client_a):
    h = _holiday()
    login_verified(client_a, _user("school_admin"))
    page = client_a.get("/attendance/holidays/").content.decode()
    assert f'action="{_delete_url(h)}"' in page
    r = client_a.post(_delete_url(h))
    assert r.status_code == 302
    assert r["Location"] == "/attendance/holidays/"
    with schema_context("school_a"):
        assert not Holiday.objects.filter(pk=h.pk).exists()
    (ev,) = _audit("attendance.holiday_deleted")
    assert ev.after == {"holiday_id": h.pk, "date": "2026-01-07"}
    assert "Founders" not in str(ev.before) + str(ev.after)


def test_holiday_delete_is_post_only_and_404s_unknown(client_a):
    h = _holiday()
    login_verified(client_a, _user("school_admin"))
    assert client_a.get(_delete_url(h)).status_code == 405
    assert client_a.post("/attendance/holidays/999999/delete/").status_code == 404
    assert client_a.post("/attendance/holidays/x1/delete/").status_code == 404
    with schema_context("school_a"):
        assert Holiday.objects.filter(pk=h.pk).exists()


@pytest.mark.parametrize("role", ["bursar", "teacher", "principal", "proprietor"])
def test_holiday_delete_forbidden_to_other_roles(client_a, role):
    h = _holiday()
    _login(client_a, _user(role), mfa=role in ("bursar", "principal"))
    assert client_a.post(_delete_url(h)).status_code == 403
    if role in ("principal", "proprietor"):
        assert "/delete/" not in client_a.get("/attendance/holidays/").content.decode()
    with schema_context("school_a"):
        assert Holiday.objects.filter(pk=h.pk).exists()
    assert _audit("attendance.holiday_deleted") == []


def test_holiday_delete_takes_holidays_lock(school_a):
    from unittest import mock

    from apps.attendance import services

    h = _holiday()
    with schema_context("school_a"):
        admin = make_staff("school_admin")
        with mock.patch("apps.attendance.services.school_lock") as lock:
            assert services.delete_holiday(h.pk, by=admin) is True
        assert services.delete_holiday(h.pk, by=admin) is False
    assert [c.args[0] for c in lock.call_args_list] == ["attendance:holidays"]


def test_concurrent_duplicate_holiday_is_form_error(school_a):
    """Two admins pass form validation for the same date; the second save is a form error,
    not an IntegrityError."""
    from apps.attendance import services
    from apps.attendance.forms import HolidayForm

    with schema_context("school_a"):
        admin = make_staff("school_admin")
        form = HolidayForm({"date": "2026-01-07", "name": "Second"})
        assert form.is_valid()
        Holiday.objects.create(date=date(2026, 1, 7), name="First")  # the other admin's
        assert services.save_holiday(form, by=admin) is None
        assert form.errors["date"] == [services.DUPLICATE]
        assert Holiday.objects.get(date=date(2026, 1, 7)).name == "First"
    assert _audit("attendance.holiday_saved") == []


@pytest.mark.parametrize("role", ["principal", "proprietor"])
def test_school_page_links_to_holidays(client_a, role):
    _login(client_a, _user(role), mfa=role == "principal")
    page = client_a.get(reverse("attendance:school")).content.decode()
    assert reverse("attendance:holidays") in page


def test_holiday_save_rechecks_records_under_the_lock(school_a):
    """A register saved after the form validated is caught at save time."""
    from apps.attendance import services
    from apps.attendance.forms import CONFLICT, HolidayForm

    with schema_context("school_a"):
        w = make_terms()
        admin = make_staff("school_admin")
        form = HolidayForm({"date": "2026-01-06", "name": "Oops"})
        assert form.is_valid()
        AttendanceRecord.objects.create(
            student=StudentProfileFactory(),
            date=date(2026, 1, 6),
            arm=w.level.arms.first(),
            status="present",
            recorded_by=admin,
        )
        assert services.save_holiday(form, by=admin) is None
        assert form.errors["date"] == [CONFLICT]
        assert not Holiday.objects.exists()


@pytest.mark.parametrize("field", ["late_threshold", "edit_window_days"])
def test_settings_db_check_constraints(school_a, field):
    from django.db import IntegrityError, transaction

    with schema_context("school_a"):
        AttendanceSettings.load()
        with pytest.raises(IntegrityError), transaction.atomic():
            AttendanceSettings.objects.filter(pk=1).update(**{field: 61})
