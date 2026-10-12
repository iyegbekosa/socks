import re
from datetime import date

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, ClassArm, ClassLevel
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.students.passwords import ALPHABET
from tests.accounts.factories import DEFAULT_PASSWORD, StaffFactory
from tests.accounts.helpers import login_verified
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


NEW_PW = "Brand-new-pass-7731"


@pytest.fixture
def admin(client_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["school_admin"], by=None)
    login_verified(client_a, u)
    return u


@pytest.fixture
def two_students(school_a):
    with schema_context("school_a"):
        students = [StudentProfileFactory(), StudentProfileFactory()]
        _make_current(students[0].enrolments.first().session)
        return students


def _make_current(session):
    from apps.academics.models import Term

    if not Term.objects.filter(is_current=True).exists():
        Term.objects.create(
            session=session,
            number=1,
            start_date=session.start_date,
            end_date=session.end_date,
            is_current=True,
        )


def _passwords(html):
    return re.findall(r'<code class="otp">([^<]+)</code>', html)


def test_issue_two_passwords_full_flow(client_a, admin, two_students):
    ids = [s.pk for s in two_students]
    r = client_a.post(reverse("students:passwords"), {"student_ids": ids})
    assert r.status_code == 200
    assert r["Cache-Control"] == "no-store" and r["Referrer-Policy"] == "no-referrer"
    pws = _passwords(r.content.decode())
    assert len(pws) == 2
    for pw in pws:
        assert len(pw) == 10 and set(pw) <= set(ALPHABET)
    with schema_context("school_a"):
        evs = AuditEvent.objects.filter(action="student.password_issued")
        assert evs.count() >= 2
        assert not any(pw in str(e.__dict__) for e in evs for pw in pws)
    from django_tenants.test.client import TenantClient

    student_client = TenantClient(client_a.tenant)
    adm = two_students[0].user.admission_number
    assert (
        student_client.post(
            reverse("accounts:login"), {"identifier": adm, "password": DEFAULT_PASSWORD}
        ).status_code
        == 200
    )  # old password dead
    with schema_context("school_a"):
        two_students[0].user.refresh_from_db()
        pw = pws[0] if two_students[0].user.check_password(pws[0]) else pws[1]
    r = student_client.post(reverse("accounts:login"), {"identifier": adm, "password": pw})
    assert r.status_code == 302
    assert student_client.get(reverse("home"))["Location"] == reverse("accounts:password_change")
    r = student_client.post(
        reverse("accounts:password_change"),
        {"old_password": pw, "new_password1": NEW_PW, "new_password2": NEW_PW},
    )
    assert r.status_code == 302
    assert student_client.get(reverse("home")).status_code == 200


def test_reissue_invalidates_earlier(client_a, admin, two_students):
    sid = two_students[0].pk
    first = _passwords(
        client_a.post(reverse("students:passwords"), {"student_ids": [sid]}).content.decode()
    )[0]
    second = _passwords(
        client_a.post(reverse("students:passwords"), {"student_ids": [sid]}).content.decode()
    )[0]
    with schema_context("school_a"):
        u = two_students[0].user
        u.refresh_from_db()
        assert u.check_password(second) and not u.check_password(first)


def test_arm_prints_active_students_in_current_session(client_a, admin, school_a):
    with schema_context("school_a"):
        a = StudentProfileFactory()
        b = StudentProfileFactory()
        other_level = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
        other_arm = ClassArm.objects.create(level=other_level, suffix="A")
        c = StudentProfileFactory(enrolment__arm=other_arm)
        arm_id = a.enrolments.first().arm_id
        old = AcademicSession.objects.create(
            name="2020/2021", start_date=date(2020, 9, 1), end_date=date(2021, 7, 1)
        )
        d = StudentProfileFactory(enrolment__session=old)
    del b, c, d
    with schema_context("school_a"):
        _make_current(a.enrolments.first().session)
    r = client_a.post(reverse("students:passwords"), {"arm": arm_id})
    assert r.status_code == 200
    assert len(_passwords(r.content.decode())) == 2


def test_get_405_and_teacher_403(client_a, admin):
    assert client_a.get(reverse("students:passwords")).status_code == 405
    with schema_context("school_a"):
        t = StaffFactory()
        set_roles(t, ["teacher"], by=None)
    client_a.force_login(t)
    assert client_a.post(reverse("students:passwords"), {"student_ids": [1]}).status_code == 403
    assert client_a.get(reverse("students:passwords")).status_code == 403


def test_neither_ids_nor_arm_redirects(client_a, admin):
    r = client_a.post(reverse("students:passwords"), {})
    assert r.status_code == 302


def test_buttons_on_detail_and_list(client_a, admin, two_students):
    s = two_students[0]
    html = client_a.get(reverse("students:detail", args=[s.pk])).content.decode()
    assert "Issue new password" in html
    arm = s.enrolments.first().arm_id
    html = client_a.get(reverse("students:list") + f"?arm={arm}").content.decode()
    assert "Print passwords for this class" in html
    assert (
        "Print passwords for this class"
        not in client_a.get(reverse("students:list")).content.decode()
    )
