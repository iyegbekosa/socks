import json
from datetime import date

import pytest
from django_tenants.utils import schema_context

from apps.attendance.models import AttendanceRecord, AttendanceSettings, Holiday
from apps.results.models import ClassResult
from apps.students.models import Enrolment
from tests.accounts.helpers import login_verified
from tests.attendance.factories import TODAY, make_register_world, today
from tests.fees.factories import make_staff

pytestmark = pytest.mark.django_db

D = "2026-01-12"


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        yield make_register_world()


def url(arm, d=D):
    return f"/api/v1/attendance/{arm.pk}/{d}"


def body(students, overrides=None):
    overrides = overrides or {}
    return {
        "records": [{"student": s.pk, "status": overrides.get(s.pk, "present")} for s in students]
    }


def post(client, u, data):
    return client.post(u, json.dumps(data), content_type="application/json")


def login(client, user):
    with schema_context("school_a"):
        client.force_login(user)


def count():
    with schema_context("school_a"):
        return AttendanceRecord.objects.count()


def test_get_new_register_has_null_statuses(client_a, w):
    login(client_a, w.teacher)
    with today():
        r = client_a.get(url(w.arm))
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["arm"] == {"id": w.arm.pk, "name": w.arm.display_name}
    assert data["date"] == D
    assert data["editable"] is True
    s = w.students[0]
    assert data["records"][0] == {
        "student": {"id": s.pk, "admission_number": s.admission_number, "name": s.full_name},
        "status": None,
    }
    assert [x["student"]["id"] for x in data["records"]] == [s.pk for s in w.students]


def test_post_saves_and_returns_register(client_a, w):
    login(client_a, w.teacher)
    s0 = w.students[0]
    with today():
        r = post(client_a, url(w.arm), body(w.students, {s0.pk: "absent"}))
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["editable"] is True
    assert [x["status"] for x in data["records"]] == ["absent", "present", "present"]
    assert count() == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b, w: b["records"].pop(),
        lambda b, w: b["records"].append(dict(b["records"][0])),
        lambda b, w: b["records"][0].update(status="asleep"),
        lambda b, w: b["records"][0].update(student="x"),
        lambda b, w: b["records"][0].update(extra=1),
        lambda b, w: b.update(extra=1),
        lambda b, w: b.update(records="nope"),
        lambda b, w: b["records"].append({"student": 999999, "status": "present"}),
    ],
    ids=["missing", "duplicate", "status", "id", "item_key", "top_key", "not_list", "extra"],
)
def test_invalid_body_is_400(client_a, w, mutate):
    login(client_a, w.teacher)
    b = body(w.students)
    mutate(b, w)
    with today():
        r = post(client_a, url(w.arm), b)
    assert r.status_code == 400, r.content
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert count() == 0


def test_weekend_and_holiday_refused(client_a, w):
    with schema_context("school_a"):
        Holiday.objects.create(date=date(2026, 1, 7), name="Founders day")
    login(client_a, w.teacher)
    with today():
        for d in ("2026-01-10", "2026-01-07"):
            r = post(client_a, url(w.arm, d), body(w.students))
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "NOT_A_SCHOOL_DAY"
            assert client_a.get(url(w.arm, d)).json()["error"]["code"] == "NOT_A_SCHOOL_DAY"
        r = post(client_a, url(w.arm, "2026-01-13"), body(w.students))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "FUTURE_DATE"
    assert count() == 0


def test_locked_is_409(client_a, w):
    with schema_context("school_a"):
        ClassResult.objects.create(arm=w.arm, term=w.term, status="approved")
    login(client_a, w.teacher)
    with today():
        assert client_a.get(url(w.arm)).json()["editable"] is False
        r = post(client_a, url(w.arm), body(w.students))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ATTENDANCE_LOCKED"
    assert count() == 0


def test_outside_edit_window_is_403(client_a, w):
    with schema_context("school_a"):
        AttendanceSettings.objects.update_or_create(pk=1, defaults={"edit_window_days": 7})
    login(client_a, w.teacher)
    with today(date(2026, 1, 16)):
        assert client_a.get(url(w.arm, "2026-01-08")).json()["editable"] is False
        r = post(client_a, url(w.arm, "2026-01-08"), body(w.students))
    assert r.status_code == 403
    assert count() == 0


def test_student_and_other_teacher_forbidden(client_a, w):
    for user in (w.students[0].user, w.teacher_b):
        login(client_a, user)
        with today():
            assert client_a.get(url(w.arm)).status_code == 403
            assert post(client_a, url(w.arm), body(w.students)).status_code == 403
    assert count() == 0


def test_principal_may_take(client_a, w):
    with schema_context("school_a"):
        principal = make_staff("principal")
    login_verified(client_a, principal)
    with today():
        assert post(client_a, url(w.arm), body(w.students)).status_code == 200
    assert count() == 3


def test_anonymous_is_401(client_a, w):
    with today():
        assert client_a.get(url(w.arm)).status_code == 401


def test_bad_date_or_arm_is_404(client_a, w):
    login(client_a, w.teacher)
    with today():
        assert client_a.get(f"/api/v1/attendance/{w.arm.pk}/2026-1-12").status_code == 404
        assert client_a.get(f"/api/v1/attendance/{w.arm.pk}/nope").status_code == 404
        assert client_a.get(f"/api/v1/attendance/x/{D}").status_code == 404
        assert client_a.get(f"/api/v1/attendance/999999/{D}").status_code == 404


def test_moved_student_shown_recorded_elsewhere(client_a, w):
    mover = w.students[0]
    login(client_a, w.teacher)
    with today():
        post(client_a, url(w.arm), body(w.students, {mover.pk: "excused"}))
    with schema_context("school_a"):
        Enrolment.objects.filter(student=mover).update(arm=w.arm_b)
    login(client_a, w.teacher_b)
    with today():
        data = client_a.get(url(w.arm_b)).json()
        assert data["records"] == [
            {
                "student": {
                    "id": mover.pk,
                    "admission_number": mover.admission_number,
                    "name": mover.full_name,
                },
                "status": "excused",
                "recorded_elsewhere": True,
            }
        ]
        r = post(client_a, url(w.arm_b), {"records": []})
        assert r.status_code == 200, r.content
        r = post(client_a, url(w.arm_b), body([mover], {mover.pk: "absent"}))
        assert r.status_code == 200, r.content
    with schema_context("school_a"):
        rec = AttendanceRecord.objects.get(student=mover, date=TODAY)
        assert (rec.arm_id, rec.status) == (w.arm.pk, "excused")
