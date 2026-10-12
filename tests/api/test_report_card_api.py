"""GET /api/v1/results/<student>/<term>/report-card: a 302 to a signed URL, or a §6.5 404."""

from types import SimpleNamespace

import cloudinary
import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, Subject, SubjectOffering
from apps.accounts.roles import set_roles
from apps.results.models import ClassResult, ReportCard
from apps.students.models import Student
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import make_card, make_sheet_world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


def staff(*roles):
    user = StaffFactory()
    set_roles(user, list(roles), by=None)
    return user


@pytest.fixture
def c(school_a):
    with schema_context("school_a"):
        w = make_sheet_world()
        st, other = w.students[0], w.students[1]
        card = make_card(st, w.term, w.arm)
        make_card(other, w.term, w.arm)
        level = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
        arm2 = ClassArm.objects.create(level=level, suffix="B")
        teacher2 = staff("teacher")
        subject = Subject.objects.create(code="ENG", name="English", sections=["jss"])
        SubjectOffering.objects.create(
            subject=subject, arm=arm2, session=w.session, teacher=teacher2
        )
        yield SimpleNamespace(w=w, st=st, other=other, card=card, other_teacher=teacher2)


def url(student_pk, term_pk) -> str:
    return f"/api/v1/results/{student_pk}/{term_pk}/report-card"


def assert_not_found(r):
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_route_name():
    assert reverse("api:report_card", args=[1, 2]) == url(1, 2)


def test_student_gets_signed_redirect_after_publish(client_a, c):
    client_a.force_login(c.st.user)
    r = client_a.get(url(c.st.pk, c.w.term.pk))
    assert r.status_code == 302
    assert r["Cache-Control"] == "no-store"
    assert r["Location"].startswith("https://api.cloudinary.com/v1_1/test/raw/download?")
    assert "signature=" in r["Location"] and "expires_at=" in r["Location"]


def test_student_before_publish_is_404(client_a, c):
    with schema_context("school_a"):
        ClassResult.objects.filter(arm=c.w.arm, term=c.w.term).update(status="approved")
    client_a.force_login(c.st.user)
    assert_not_found(client_a.get(url(c.st.pk, c.w.term.pk)))


def test_student_cannot_reach_another_students_card(client_a, c):
    client_a.force_login(c.st.user)
    assert_not_found(client_a.get(url(c.other.pk, c.w.term.pk)))


def test_teacher_of_another_arm_is_404(client_a, c):
    client_a.force_login(c.other_teacher)
    assert_not_found(client_a.get(url(c.st.pk, c.w.term.pk)))


def test_subject_teacher_and_admin_get_redirect(client_a, c):
    client_a.force_login(c.w.teacher)
    assert client_a.get(url(c.st.pk, c.w.term.pk)).status_code == 302
    login_verified(client_a, staff("school_admin"))
    r = client_a.get(url(c.other.pk, c.w.term.pk))
    assert r.status_code == 302 and r["Cache-Control"] == "no-store"


def test_missing_card_is_404(client_a, c):
    with schema_context("school_a"):
        ReportCard.objects.filter(pk=c.card.pk).delete()
    login_verified(client_a, staff("school_admin"))
    assert_not_found(client_a.get(url(c.st.pk, c.w.term.pk)))


@pytest.mark.parametrize("student,term", [("x", "1"), ("9" * 19, "1"), ("1", "1.0")])
def test_bad_ids_are_404(client_a, c, student, term):
    login_verified(client_a, staff("school_admin"))
    assert_not_found(client_a.get(url(student, term)))


def test_anonymous_is_refused(client_a, c):
    r = client_a.get(url(c.st.pk, c.w.term.pk))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "NOT_AUTHENTICATED"
    assert "Location" not in r


def test_admin_in_a_cannot_reach_school_b_card(client_a, school_a, school_b):
    with schema_context("school_b"):
        w = make_sheet_world()
        st_b = StudentProfileFactory(pk=987654)
        card_b = make_card(st_b, w.term, w.arm)
    with schema_context("school_a"):
        make_sheet_world()
        assert not Student.objects.filter(pk=987654).exists()
        admin = staff("school_admin")
    login_verified(client_a, admin)
    r = client_a.get(url(card_b.student_id, card_b.term_id))
    assert_not_found(r)
    assert "Location" not in r
