from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import AcademicSession, Term
from apps.academics.services import (
    create_session,
    current_session,
    current_term,
    set_current_term,
)
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db

TERMS = [
    (date(2026, 9, 14), date(2026, 12, 18)),
    (date(2027, 1, 11), date(2027, 4, 2)),
    (date(2027, 4, 26), date(2027, 7, 23)),
]


@pytest.fixture(autouse=True)
def _clean(school_a):
    with schema_context("school_a"):
        Term.objects.all().delete()
        AcademicSession.objects.all().delete()
    yield
    with schema_context("school_a"):
        Term.objects.all().delete()
        AcademicSession.objects.all().delete()


@pytest.fixture
def admin_user(school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["school_admin"], by=u)
    return u


def make(name="2026/2027", terms=None, by=None):
    return create_session(name=name, terms=terms or TERMS, by=by)


def test_create_session(admin_user):
    with schema_context("school_a"):
        s = make(by=admin_user)
        assert s.start_date == date(2026, 9, 14)
        assert s.end_date == date(2027, 7, 23)
        assert [t.number for t in s.terms.order_by("number")] == [1, 2, 3]
        assert Term.objects.get(session=s, number=2).get_number_display() == "2nd Term"
        ev = AuditEvent.objects.get(action="academics.session_created")
        assert ev.after["name"] == "2026/2027"
        assert [t["number"] for t in ev.after["terms"]] == [1, 2, 3]


@pytest.mark.parametrize("name", ["2026/2028", "2026-2027", "26/27", ""])
def test_bad_names(admin_user, name):
    with schema_context("school_a"):
        with pytest.raises(ValidationError) as e:
            make(name=name, by=admin_user)
        assert "name" in e.value.message_dict
        assert AcademicSession.objects.count() == 0


def test_two_terms_rejected(admin_user):
    with schema_context("school_a"):
        with pytest.raises(ValidationError, match="three terms"):
            make(terms=TERMS[:2], by=admin_user)


def test_overlapping_terms(admin_user):
    bad = [TERMS[0], (date(2026, 12, 1), date(2027, 4, 2)), TERMS[2]]
    with schema_context("school_a"):
        with pytest.raises(ValidationError) as e:
            make(terms=bad, by=admin_user)
        assert "term_2" in e.value.message_dict


def test_term_ends_before_start(admin_user):
    bad = [(date(2026, 12, 18), date(2026, 9, 14)), TERMS[1], TERMS[2]]
    with schema_context("school_a"):
        with pytest.raises(ValidationError) as e:
            make(terms=bad, by=admin_user)
        assert "before" in e.value.message_dict["term_1"][0]


def test_overlapping_session_and_duplicate_name(admin_user):
    with schema_context("school_a"):
        make(by=admin_user)
        with pytest.raises(ValidationError):
            make(by=admin_user)
        shifted = [(date(2027, 5, 1), date(2027, 8, 1)), (date(2027, 9, 1), date(2027, 10, 1))]
        shifted.append((date(2027, 11, 1), date(2027, 12, 1)))
        with pytest.raises(ValidationError) as e:
            make(name="2027/2028", terms=shifted, by=admin_user)
        assert "overlap" in str(e.value.message_dict["name"])
        assert AcademicSession.objects.count() == 1


def test_set_current_twice_leaves_one(admin_user):
    with schema_context("school_a"):
        s = make(by=admin_user)
        t1, t2 = s.terms.get(number=1), s.terms.get(number=2)
        set_current_term(t1, by=admin_user)
        set_current_term(t2, by=admin_user)
        assert Term.objects.filter(is_current=True).count() == 1
        assert current_term() == t2
        assert AuditEvent.objects.filter(action="academics.term_set_current").count() == 2


def test_db_rejects_second_current(admin_user):
    with schema_context("school_a"):
        s = make(by=admin_user)
        set_current_term(s.terms.get(number=1), by=admin_user)
        with pytest.raises(IntegrityError), transaction.atomic():
            Term.objects.filter(number=2).update(is_current=True)


def test_current_session_follows_term(admin_user):
    with schema_context("school_a"):
        assert current_term() is None
        assert current_session() is None
        s = make(by=admin_user)
        set_current_term(s.terms.get(number=3), by=admin_user)
        assert current_session() == s


def test_teacher_forbidden(client_a, school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["teacher"], by=u)
    with schema_context("school_a"):
        client_a.force_login(u)
    assert client_a.get(reverse("academics:session_create")).status_code == 403
    assert client_a.get(reverse("academics:sessions")).status_code == 403


def test_principal_reads_but_cannot_write(client_a, school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["principal"], by=u)
    login_verified(client_a, u)
    assert client_a.get(reverse("academics:sessions")).status_code == 200
    assert client_a.get(reverse("academics:session_create")).status_code == 403
    assert client_a.post(reverse("academics:session_create"), {}).status_code == 403


def test_admin_creates_and_sets_current_via_pages(client_a, admin_user):
    login_verified(client_a, admin_user)
    data = {
        "name": "2026/2027",
        "term_1_start": "14/09/2026",
        "term_1_end": "18/12/2026",
        "term_2_start": "11/01/2027",
        "term_2_end": "02/04/2027",
        "term_3_start": "26/04/2027",
        "term_3_end": "23/07/2027",
    }
    r = client_a.post(reverse("academics:session_create"), data)
    assert r.status_code == 302
    with schema_context("school_a"):
        term = Term.objects.get(number=1)
        assert term.start_date == date(2026, 9, 14)
        assert AuditEvent.objects.filter(action="academics.session_created").count() == 1
    html = client_a.get(reverse("academics:sessions")).content.decode()
    assert "2026/2027" in html
    assert "14/09/2026" in html
    url = reverse("academics:set_current", args=[term.pk])
    assert client_a.get(url).status_code == 405
    assert client_a.post(url).status_code == 302
    with schema_context("school_a"):
        assert current_term().pk == term.pk
        assert AuditEvent.objects.filter(action="academics.term_set_current").count() == 1
    assert "Current" in client_a.get(reverse("academics:sessions")).content.decode()


def test_form_shows_errors(client_a, admin_user):
    login_verified(client_a, admin_user)
    data = {
        "name": "2026/2028",
        "term_1_start": "14/09/2026",
        "term_1_end": "18/12/2026",
        "term_2_start": "11/01/2027",
        "term_2_end": "02/04/2027",
        "term_3_start": "26/04/2027",
        "term_3_end": "23/07/2027",
    }
    r = client_a.post(reverse("academics:session_create"), data)
    assert r.status_code == 200
    assert "must be" in r.content.decode()
    with schema_context("school_a"):
        assert AcademicSession.objects.count() == 0


def test_home_link(client_a, admin_user):
    login_verified(client_a, admin_user)
    assert "Academic setup" in client_a.get("/").content.decode()
