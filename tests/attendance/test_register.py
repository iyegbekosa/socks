from datetime import date
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, FormTeacher
from apps.accounts.roles import set_roles
from apps.attendance import register
from apps.attendance.models import AttendanceRecord, AttendanceSettings, Holiday
from apps.audit.models import AuditEvent
from apps.results.models import ClassResult
from apps.students.models import Enrolment
from tests.accounts.helpers import login_verified
from tests.attendance.factories import TODAY, make_register_world, today, version_of
from tests.fees.factories import make_staff
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

D = "2026-01-12"


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield make_register_world()


@pytest.fixture
def w40(school_a):
    with schema_context("school_a"):
        yield make_register_world(40)


def _url(arm, d=D):
    return reverse("attendance:register", args=[arm.pk, d])


def _post_data(students, overrides=None, *, version=None):
    """The register form. ``version``: the hidden token (none: the form of a new register)."""
    overrides = overrides or {}
    data = {f"s_{s.pk}": overrides.get(s.pk, "present") for s in students}
    if version is not None:
        data["version"] = version
    return data


def _audit(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action).order_by("pk"))


def _records(**kw):
    with schema_context("school_a"):
        return {r.student_id: r for r in AttendanceRecord.objects.filter(**kw)}


def _login(client, user):
    with schema_context("school_a"):
        client.force_login(user)


def test_urls():
    assert reverse("attendance:register", args=[3, D]) == f"/attendance/register/3/{D}/"
    assert reverse("attendance:today", args=[3]) == "/attendance/register/3/"
    assert reverse("api:attendance_register", args=[3, D]) == f"/api/v1/attendance/3/{D}"


# Per-request work outside the view: tenant lookup, session, user and the roles middleware
# reads, plus the test-only noise (SET search_path before each query, and SAVEPOINTs because
# the test runs inside a transaction; in production the outer atomic() is BEGIN/COMMIT).
NOISE = ("SET search_path", "SAVEPOINT", "RELEASE SAVEPOINT")


def _real(ctx):
    return [q["sql"] for q in ctx.captured_queries if not q["sql"].startswith(NOISE)]


def _view_queries(client, method, url, data=None):
    """(response, queries the view itself made): the request minus a baseline request that
    passes the same middleware and 404s before the view queries anything."""
    with CaptureQueriesContext(connection) as base:
        assert client.get(f"/attendance/register/x/{D}/").status_code == 404
    with CaptureQueriesContext(connection) as ctx:
        r = getattr(client, method)(url, data or {})
    return r, len(_real(ctx)) - len(_real(base))


def test_form_teacher_saves_40_student_register(client_a, w40):
    w = w40
    _login(client_a, w.teacher)
    client_a.get(reverse("home"))  # warm the session
    overrides = {s.pk: "absent" for s in w.students[:3]}
    overrides.update({s.pk: "late" for s in w.students[3:5]})
    with today():
        r, n = _view_queries(client_a, "get", _url(w.arm))
        assert r.status_code == 200
        assert n <= 12, n
        html = r.content.decode()
        assert html.count('value="present" checked') == 40
        assert len(r.content) < 30_000, len(r.content)
        assert 'register.js" defer' in html
        r, n = _view_queries(client_a, "post", _url(w.arm), _post_data(w.students, overrides))
        assert n <= 12, n
        # The edit path (UPDATE instead of INSERT) stays within budget too.
        edit, n = _view_queries(
            client_a,
            "post",
            _url(w.arm),
            _post_data(
                w.students, {**overrides, w.students[0].pk: "late"}, version=version_of(w.arm)
            ),
        )
        assert edit.status_code == 302
        assert n <= 12, n
    assert r.status_code == 302
    assert r["Location"] == _url(w.arm)
    recs = _records(date=TODAY)
    assert len(recs) == 40
    assert recs.pop(w.students[0].pk).status == "late"  # edited above
    assert {r.arm_id for r in recs.values()} == {w.arm.pk}
    assert {r.recorded_by_id for r in recs.values()} == {w.teacher.pk}
    assert sum(r.status == "absent" for r in recs.values()) == 2
    (ev,) = _audit("attendance.register_saved")
    assert ev.after == {
        "arm_id": w.arm.pk,
        "date": D,
        "counts": {"present": 35, "absent": 3, "late": 2, "excused": 0},
    }
    (changed,) = _audit("attendance.register_changed")
    assert changed.after == {"date": D, str(w.students[0].pk): "late"}
    with today():
        page = client_a.get(_url(w.arm)).content.decode()
    assert "Register saved for 12 January 2026" in page
    assert page.count('value="absent" checked') == 2


def test_resave_with_one_change_audits_only_that_student(client_a, w):
    _login(client_a, w.teacher)
    s0, s1, _ = w.students
    with today():
        client_a.post(_url(w.arm), _post_data(w.students))
        r = client_a.post(
            _url(w.arm), _post_data(w.students, {s1.pk: "late"}, version=version_of(w.arm))
        )
    assert r.status_code == 302
    (ev,) = _audit("attendance.register_changed")
    assert ev.before == {str(s1.pk): "present"}
    assert ev.after == {"date": D, str(s1.pk): "late"}
    assert len(_audit("attendance.register_saved")) == 1
    assert _records(date=TODAY)[s1.pk].status == "late"


def test_resave_without_changes_writes_no_audit(client_a, w):
    _login(client_a, w.teacher)
    with today():
        client_a.post(_url(w.arm), _post_data(w.students))
        r = client_a.post(_url(w.arm), _post_data(w.students, version=version_of(w.arm)))
    assert r.status_code == 302
    assert len(_audit("attendance.register_saved")) == 1
    assert _audit("attendance.register_changed") == []


def test_other_arm_teacher_forbidden(client_a, w):
    _login(client_a, w.teacher_b)
    with today():
        assert client_a.get(_url(w.arm)).status_code == 403
        assert client_a.post(_url(w.arm), _post_data(w.students)).status_code == 403
    assert _records() == {}


@pytest.mark.parametrize("role", ["teacher", "bursar", "proprietor"])
def test_other_staff_forbidden(client_a, w, role):
    with schema_context("school_a"):
        user = make_staff(role)
    login_verified(client_a, user)
    with today():
        assert client_a.get(_url(w.arm)).status_code == 403


def test_student_forbidden(client_a, w):
    _login(client_a, w.students[0].user)
    with today():
        assert client_a.get(_url(w.arm)).status_code == 403


@pytest.mark.parametrize("role", ["principal", "school_admin"])
def test_principal_and_admin_may_take(client_a, w, role):
    with schema_context("school_a"):
        user = make_staff(role)
    login_verified(client_a, user)
    with today():
        assert client_a.get(_url(w.arm)).status_code == 200
        assert client_a.post(_url(w.arm), _post_data(w.students)).status_code == 302
    assert {r.recorded_by_id for r in _records(date=TODAY).values()} == {user.pk}


def test_edit_window_for_form_teacher_only(client_a, w):
    with schema_context("school_a"):
        AttendanceSettings.objects.update_or_create(pk=1, defaults={"edit_window_days": 7})
        principal = make_staff("principal")
    _login(client_a, w.teacher)
    friday = date(2026, 1, 16)
    with today(friday):
        # 7 days back is inside the window; 8 days back is not, even for a first save.
        assert client_a.post(_url(w.arm, "2026-01-09"), _post_data(w.students)).status_code == 302
        r = client_a.post(_url(w.arm, "2026-01-08"), _post_data(w.students))
        assert r.status_code == 403
        page = client_a.get(_url(w.arm, "2026-01-08"))
        assert page.status_code == 200
        assert 'type="radio"' not in page.content.decode()
    assert _records(date=date(2026, 1, 8)) == {}
    login_verified(client_a, principal)
    with today(friday):
        r = client_a.post(_url(w.arm, "2026-01-08"), _post_data(w.students))
    assert r.status_code == 302
    assert len(_records(date=date(2026, 1, 8))) == 3


def test_register_locked_once_results_leave_draft(client_a, w):
    _login(client_a, w.teacher)
    with today():
        client_a.post(_url(w.arm), _post_data(w.students))
    with schema_context("school_a"):
        cr = ClassResult.objects.create(arm=w.arm, term=w.term, status="reviewed")
        assert register.is_locked(w.arm, w.term)
    with today():
        r = client_a.post(_url(w.arm), _post_data(w.students, {w.students[0].pk: "absent"}))
        assert r.status_code == 409
        page = client_a.get(_url(w.arm)).content.decode()
    assert 'type="radio"' not in page
    assert "locked" in page
    assert _records(date=TODAY)[w.students[0].pk].status == "present"
    # A results unlock re-opens it.
    with schema_context("school_a"):
        ClassResult.objects.filter(pk=cr.pk).update(status="draft")
        assert not register.is_locked(w.arm, w.term)
    with today():
        r = client_a.post(
            _url(w.arm),
            _post_data(w.students, {w.students[0].pk: "absent"}, version=version_of(w.arm)),
        )
    assert r.status_code == 302


def test_weekend_and_holiday_refused(client_a, w):
    with schema_context("school_a"):
        Holiday.objects.create(date=date(2026, 1, 7), name="Founders day")
    _login(client_a, w.teacher)
    with today():
        for d in ("2026-01-10", "2026-01-07"):
            r = client_a.get(_url(w.arm, d))
            assert r.status_code == 400
            assert "That date is not a school day." in r.content.decode()
            r = client_a.post(_url(w.arm, d), _post_data(w.students))
            assert r.status_code == 400
        r = client_a.get(_url(w.arm, "2026-01-13"))
        assert r.status_code == 400
        assert "A register cannot be taken for a future date." in r.content.decode()
    assert _records() == {}


def test_bad_date_or_arm_is_404(client_a, w):
    _login(client_a, w.teacher)
    with today():
        assert client_a.get(f"/attendance/register/{w.arm.pk}/2026-13-01/").status_code == 404
        assert client_a.get(f"/attendance/register/x{w.arm.pk}/{D}/").status_code == 404
        assert client_a.get(f"/attendance/register/999999/{D}/").status_code == 404


@pytest.mark.parametrize("case", ["missing", "extra", "bad_status", "bad_id"])
def test_missing_or_extra_student_is_400(client_a, w, case):
    _login(client_a, w.teacher)
    data = _post_data(w.students)
    if case == "missing":
        data.pop(f"s_{w.students[0].pk}")
    elif case == "extra":
        with schema_context("school_a"):
            other = StudentProfileFactory(enrolment__arm=w.arm_b)
        data[f"s_{other.pk}"] = "present"
    elif case == "bad_status":
        data[f"s_{w.students[0].pk}"] = "asleep"
    else:
        data["s_x1"] = "present"
    with today():
        r = client_a.post(_url(w.arm), data)
    assert r.status_code == 400
    assert _records() == {}


def test_students_for_lists_active_enrolments_in_name_order(w):
    with schema_context("school_a"):
        withdrawn = StudentProfileFactory(enrolment__arm=w.arm, enrolment__status="withdrawn")
        left = StudentProfileFactory(enrolment__arm=w.arm, status="withdrawn")
        first = StudentProfileFactory(last_name="Aaa", first_name="Zed", enrolment__arm=w.arm)
        second = StudentProfileFactory(last_name="Aaa", first_name="Bob", enrolment__arm=w.arm)
        got = register.students_for(w.arm, TODAY)
    assert withdrawn not in got and left not in got
    assert got[:2] == [second, first]
    assert got[2:] == w.students


def test_moved_student_keeps_old_records_and_appears_in_new_arm(client_a, w):
    mover = w.students[0]
    day1, day2 = "2026-01-12", "2026-01-13"
    _login(client_a, w.teacher)
    with today(date(2026, 1, 12)):
        client_a.post(_url(w.arm, day1), _post_data(w.students, {mover.pk: "late"}))
    with schema_context("school_a"):
        Enrolment.objects.filter(student=mover).update(arm=w.arm_b)
        before = {r.pk: (r.arm_id, r.status) for r in AttendanceRecord.objects.filter(arm=w.arm)}
    _login(client_a, w.teacher_b)
    with today(date(2026, 1, 13)):
        page = client_a.get(_url(w.arm_b, day1)).content.decode()
        assert f"Recorded in {w.arm.display_name}: Late" in page
        assert f'name="s_{mover.pk}"' not in page
        # The entry for the moved student is ignored, not an error, and not overwritten.
        r = client_a.post(_url(w.arm_b, day1), {f"s_{mover.pk}": "absent"})
        assert r.status_code == 302
        r = client_a.post(_url(w.arm_b, day2), {f"s_{mover.pk}": "absent"})
        assert r.status_code == 302
    with schema_context("school_a"):
        after = {r.pk: (r.arm_id, r.status) for r in AttendanceRecord.objects.filter(arm=w.arm)}
        assert after == before
        assert AttendanceRecord.objects.filter(arm=w.arm_b, date=date(2026, 1, 12)).count() == 0
        rec = AttendanceRecord.objects.get(student=mover, date=date(2026, 1, 13))
        assert (rec.arm_id, rec.status) == (w.arm_b.pk, "absent")
    # The old arm's register no longer lists the mover.
    _login(client_a, w.teacher)
    with today(date(2026, 1, 13)):
        page = client_a.get(_url(w.arm, day2)).content.decode()
    assert f'name="s_{mover.pk}"' not in page


def test_newly_absent_records_go_to_the_hook(w, django_capture_on_commit_callbacks):
    s0, s1, s2 = w.students
    with schema_context("school_a"), today():
        with (
            mock.patch("apps.attendance.hooks.on_register_saved") as hook,
            django_capture_on_commit_callbacks(execute=True),
        ):
            res = register.save_register(
                w.arm, TODAY, {s0.pk: "absent", s1.pk: "present", s2.pk: "excused"}, by=w.teacher
            )
        recs = {r.student_id: r.pk for r in AttendanceRecord.objects.all()}
        assert res.created == 3
        assert res.newly_absent == [recs[s0.pk]]
        hook.assert_called_once_with([recs[s0.pk]])
        with (
            mock.patch("apps.attendance.hooks.on_register_saved") as hook,
            django_capture_on_commit_callbacks(execute=True),
        ):
            res = register.save_register(
                w.arm, TODAY, {s0.pk: "absent", s1.pk: "absent", s2.pk: "present"}, by=w.teacher
            )
        assert res.created == 0
        assert res.changed == {s1.pk: ("present", "absent"), s2.pk: ("excused", "present")}
        assert res.newly_absent == [recs[s1.pk]]
        hook.assert_called_once_with([recs[s1.pk]])
        with (
            mock.patch("apps.attendance.hooks.on_register_saved") as hook,
            django_capture_on_commit_callbacks(execute=True),
        ):
            register.save_register(
                w.arm, TODAY, {s0.pk: "absent", s1.pk: "late", s2.pk: "present"}, by=w.teacher
            )
        hook.assert_not_called()


def test_save_takes_holiday_lock_then_register_lock_and_rechecks_date(w):
    with schema_context("school_a"), today():
        with CaptureQueriesContext(connection) as ctx:
            register.save_register(
                w.arm, TODAY, {s.pk: "present" for s in w.students}, by=w.teacher
            )
        sql = [q["sql"] for q in ctx.captured_queries]
        locks = [i for i, s in enumerate(sql) if "pg_advisory_xact_lock" in s]
        holiday_checks = [i for i, s in enumerate(sql) if '"attendance_holiday"' in s]
        assert len(locks) == 2
        assert holiday_checks and holiday_checks[0] > locks[1]
        # A holiday added (by a save that held the lock) before ours ran is respected.
        Holiday.objects.create(date=date(2026, 1, 13), name="Snap")
    with schema_context("school_a"), today(date(2026, 1, 13)):
        with pytest.raises(register.RegisterDateError):
            register.save_register(
                w.arm, date(2026, 1, 13), {s.pk: "present" for s in w.students}, by=w.teacher
            )


def test_lock_names(w):
    with schema_context("school_a"), today():
        with mock.patch("apps.attendance.register.school_lock") as lock:
            register.save_register(
                w.arm, TODAY, {s.pk: "present" for s in w.students}, by=w.teacher
            )
    assert [c.args[0] for c in lock.call_args_list] == [
        "attendance:holidays",
        f"attendance:{w.arm.pk}:{D}",
    ]


def test_today_redirects_to_todays_register(client_a, w):
    _login(client_a, w.teacher)
    with today():
        r = client_a.get(reverse("attendance:today", args=[w.arm.pk]))
    assert r.status_code == 302
    assert r["Location"] == _url(w.arm)


def test_form_teacher_home_links_to_todays_register(client_a, w):
    _login(client_a, w.teacher)
    html = client_a.get(reverse("home")).content.decode()
    assert "Take today&#x27;s register" in html or "Take today's register" in html
    assert reverse("attendance:today", args=[w.arm.pk]) in html
    assert reverse("attendance:today", args=[w.arm_b.pk]) not in html


@pytest.mark.parametrize("role", ["principal", "school_admin"])
def test_arm_page_links_to_register(client_a, w, role):
    with schema_context("school_a"):
        user = make_staff(role)
    login_verified(client_a, user)
    html = client_a.get(reverse("academics:arm_detail", args=[w.arm.pk])).content.decode()
    assert reverse("attendance:today", args=[w.arm.pk]) in html


def test_arm_page_hides_register_link_from_proprietor(client_a, w):
    with schema_context("school_a"):
        user = make_staff("proprietor")
    login_verified(client_a, user)
    html = client_a.get(reverse("academics:arm_detail", args=[w.arm.pk])).content.decode()
    assert reverse("attendance:today", args=[w.arm.pk]) not in html


def test_locked_beats_edit_window(client_a, w):
    """Locked is 409 for everyone, even a form teacher outside the edit window."""
    with schema_context("school_a"):
        ClassResult.objects.create(arm=w.arm, term=w.term, status="approved")
    _login(client_a, w.teacher)
    with today(date(2026, 1, 16)):
        r = client_a.post(_url(w.arm, "2026-01-05"), _post_data(w.students))
    assert r.status_code == 409
    assert _records() == {}


def test_former_teacher_keeps_no_access(client_a, w):
    """A FormTeacher row outlives a removed teacher role; access must not."""
    with schema_context("school_a"):
        set_roles(w.teacher, [], by=None)
    _login(client_a, w.teacher)
    with today():
        assert client_a.get(_url(w.arm)).status_code == 403
        assert client_a.post(_url(w.arm), _post_data(w.students)).status_code == 403
    assert _records() == {}


def test_previous_session_form_teacher_forbidden(client_a, w):
    with schema_context("school_a"):
        old = AcademicSession.objects.create(
            name="2024/2025", start_date=date(2024, 9, 1), end_date=date(2025, 7, 31)
        )
        former = make_staff("teacher")
        FormTeacher.objects.create(arm=w.arm_b, session=old, teacher=former)
        FormTeacher.objects.filter(arm=w.arm, session=w.session).update(teacher=w.teacher_b)
        FormTeacher.objects.create(arm=w.arm, session=old, teacher=former)
    _login(client_a, former)
    with today():
        assert client_a.get(_url(w.arm)).status_code == 403


@pytest.mark.parametrize("role", ["principal", "school_admin"])
def test_locked_for_principal_and_admin_too(client_a, w, role):
    with schema_context("school_a"):
        ClassResult.objects.create(arm=w.arm, term=w.term, status="approved")
        user = make_staff(role)
    login_verified(client_a, user)
    with today():
        assert client_a.post(_url(w.arm), _post_data(w.students)).status_code == 409
        assert 'type="radio"' not in client_a.get(_url(w.arm)).content.decode()
    assert _records() == {}


def test_past_term_date_refused(client_a, w):
    with schema_context("school_a"):
        principal = make_staff("principal")
    login_verified(client_a, principal)
    with today():
        r = client_a.post(_url(w.arm, "2025-12-01"), _post_data(w.students))
    assert r.status_code == 400
    assert "That date is not a school day." in r.content.decode()
    assert _records() == {}


# --- Navigating dates (Ruling X) ---------------------------------------------------------


def _today_url(arm, query=""):
    return reverse("attendance:today", args=[arm.pk]) + query


def test_register_page_has_date_picker_and_school_day_links(client_a, w):
    with schema_context("school_a"):
        Holiday.objects.create(date=date(2026, 1, 8), name="Break")
    _login(client_a, w.teacher)
    with today():
        html = client_a.get(_url(w.arm, "2026-01-09")).content.decode()
    assert 'type="date"' in html
    assert 'min="2026-01-05"' in html and 'max="2026-01-12"' in html
    assert 'value="2026-01-09"' in html
    assert f'action="{_today_url(w.arm)}"' in html
    # Previous skips the holiday; next skips the weekend.
    assert f'href="{_url(w.arm, "2026-01-07")}"' in html
    assert f'href="{_url(w.arm, "2026-01-12")}"' in html
    assert "previous school day" in html and "next school day" in html


def test_next_link_hidden_on_today(client_a, w):
    _login(client_a, w.teacher)
    with today():
        html = client_a.get(_url(w.arm)).content.decode()
    assert f'href="{_url(w.arm, "2026-01-09")}"' in html
    assert "next school day" not in html


def test_first_day_of_term_has_no_previous_link(client_a, w):
    _login(client_a, w.teacher)
    with today():
        html = client_a.get(_url(w.arm, "2026-01-05")).content.decode()
    assert "previous school day" not in html
    assert f'href="{_url(w.arm, "2026-01-06")}"' in html


def test_date_picker_opens_chosen_date(client_a, w):
    _login(client_a, w.teacher)
    with today():
        r = client_a.get(_today_url(w.arm, "?date=2026-01-07"))
        assert r.status_code == 302
        assert r["Location"] == _url(w.arm, "2026-01-07")
        # A garbled date falls back to today.
        r = client_a.get(_today_url(w.arm, "?date=7/1/2026"))
        assert r["Location"] == _url(w.arm)


def test_non_school_day_page_keeps_navigation(client_a, w):
    _login(client_a, w.teacher)
    with today():
        r = client_a.get(_url(w.arm, "2026-01-10"))  # Saturday
    assert r.status_code == 400
    html = r.content.decode()
    assert "That date is not a school day." in html
    assert 'type="date"' in html
    assert f'href="{_url(w.arm, "2026-01-09")}"' in html
    assert f'href="{_url(w.arm, "2026-01-12")}"' in html


def test_today_on_weekend_redirects_to_last_school_day(client_a, w):
    with schema_context("school_a"):
        Holiday.objects.create(date=date(2026, 1, 9), name="Break")
    _login(client_a, w.teacher)
    with today(date(2026, 1, 10)):  # Saturday; Friday is a holiday
        r = client_a.get(_today_url(w.arm))
        assert r.status_code == 302
        assert r["Location"] == _url(w.arm, "2026-01-08")
        html = client_a.get(r["Location"]).content.decode()
    assert "Today is not a school day — showing 8 January 2026." in html


def test_today_with_no_school_day_yet_is_friendly(client_a, w):
    _login(client_a, w.teacher)
    with today(date(2026, 1, 4)):  # Sunday before the term's first day
        r = client_a.get(_today_url(w.arm))
    assert r.status_code == 200
    assert "There has been no school day yet this term." in r.content.decode()


def test_today_with_no_current_term_is_friendly(client_a, w):
    with schema_context("school_a"):
        from apps.academics.models import Term

        Term.objects.update(is_current=False)
        principal = make_staff("principal")
    login_verified(client_a, principal)
    with today():
        r = client_a.get(_today_url(w.arm))
    assert r.status_code == 200
    assert "There is no current term." in r.content.decode()


def test_unsaved_register_says_not_yet_taken(client_a, w):
    _login(client_a, w.teacher)
    banner = "Not yet taken — everyone defaults to present"
    with today():
        assert banner in client_a.get(_url(w.arm)).content.decode()
        client_a.post(_url(w.arm), _post_data(w.students))
        assert banner not in client_a.get(_url(w.arm)).content.decode()


# --- Stale forms and concurrent saves (Ruling Z) ------------------------------------------

STALE = "This register was changed by someone else — reload to see the latest."
RACE = "Someone else saved this register at the same time — reload."


def _principal(client):
    with schema_context("school_a"):
        user = make_staff("principal")
    login_verified(client, user)
    return user


def test_form_carries_version(client_a, w):
    _login(client_a, w.teacher)
    with today():
        r = client_a.get(_url(w.arm))
        assert '<input type="hidden" name="version" value="new">' in r.content.decode()
        client_a.post(_url(w.arm), _post_data(w.students))
        v = version_of(w.arm)
        assert v != "new"
        assert f'name="version" value="{v}"' in client_a.get(_url(w.arm)).content.decode()


def test_stale_principal_post_after_teacher_save_is_409(school_a, client_a, w):
    """The principal opened the new register before the teacher saved it; the principal's
    all-present form must not overwrite the teacher's absence."""
    from django_tenants.test.client import TenantClient

    teacher = TenantClient(school_a)
    _login(teacher, w.teacher)
    _principal(client_a)
    s0 = w.students[0]
    with today():
        assert 'name="version" value="new"' in client_a.get(_url(w.arm)).content.decode()
        assert (
            teacher.post(_url(w.arm), _post_data(w.students, {s0.pk: "absent"})).status_code == 302
        )
        r = client_a.post(_url(w.arm), _post_data(w.students, version="new"))
    assert r.status_code == 409
    html = r.content.decode()
    assert STALE in html
    # The latest register is shown, with its fresh version, not the stale form.
    assert html.count('value="absent" checked') == 1
    assert f'name="version" value="{version_of(w.arm)}"' in html
    assert _records(date=TODAY)[s0.pk].status == "absent"
    assert _audit("attendance.register_changed") == []

    # Stale after an edit too: the principal reloads, the teacher edits, the principal saves.
    with today():
        loaded = version_of(w.arm)
        assert (
            teacher.post(
                _url(w.arm), _post_data(w.students, {s0.pk: "late"}, version=loaded)
            ).status_code
            == 302
        )
        r = client_a.post(_url(w.arm), _post_data(w.students, version=loaded))
        assert r.status_code == 409
        # A form without a version cannot edit an existing register either.
        assert client_a.post(_url(w.arm), _post_data(w.students)).status_code == 409
        ok = client_a.post(_url(w.arm), _post_data(w.students, version=version_of(w.arm)))
    assert ok.status_code == 302
    assert _records(date=TODAY)[s0.pk].status == "present"


def _race_with_arm_b(w):
    """Patch the register read so that, once, another arm records students[0] for the date
    between this save's read and its insert (a moved student on two registers)."""
    real = register._listed_with_records
    done = []

    def racing(arm, d, term):
        out = real(arm, d, term)
        if not done:
            done.append(1)
            AttendanceRecord.objects.create(
                student=w.students[0], date=d, arm=w.arm_b, status="absent", recorded_by=w.teacher_b
            )
        return out

    return mock.patch.object(register, "_listed_with_records", side_effect=racing)


def test_concurrent_arm_move_save_is_conflict_not_500(w):
    with schema_context("school_a"), today(), _race_with_arm_b(w):
        with pytest.raises(register.RegisterConflict) as exc:
            register.save_register(
                w.arm, TODAY, {s.pk: "present" for s in w.students}, by=w.teacher
            )
        assert exc.value.message == RACE and exc.value.stale is False
        assert not AttendanceRecord.objects.filter(arm=w.arm).exists()


def test_concurrent_arm_move_save_is_409_on_html(client_a, w):
    _login(client_a, w.teacher)
    with today(), _race_with_arm_b(w):
        r = client_a.post(_url(w.arm), _post_data(w.students))
    assert r.status_code == 409
    assert RACE in r.content.decode()
    assert _records(arm=w.arm) == {}
