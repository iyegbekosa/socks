"""A student's own published results (``results:my_results``)."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.timezone import now
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, Subject, SubjectOffering
from apps.results.models import ClassResult, ReportCard, ResultSummary, SubjectResult
from tests.results.factories import accuracy_world, approve_and_compute
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

MY_RESULTS = "/results/me/"


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = accuracy_world()
        approve_and_compute(world)
        yield world


def set_status(w, status):
    ClassResult.objects.filter(arm=w.arm, term=w.term).update(status=status)


def page(client, student):
    client.force_login(student.user)
    r = client.get(reverse("results:my_results"))
    assert r.status_code == 200
    return r


def test_url():
    assert reverse("results:my_results") == MY_RESULTS


def test_nothing_before_publish(client_a, w):
    r = page(client_a, w.students["A"])
    html = r.content.decode()
    assert r.context["terms"] == []
    assert "No published results yet" in html
    assert "2nd" not in html and "230.0" not in html and "76.67" not in html


def test_shows_own_results_after_publish(client_a, w):
    set_status(w, "published")
    st = w.students["A"]
    ReportCard.objects.create(
        student=st, term=w.term, arm=w.arm, file_name="report_cards/x.pdf", generated_at=now()
    )
    r = page(client_a, st)
    html = r.content.decode()
    [entry] = r.context["terms"]
    assert entry["summary"].student_id == st.pk
    assert "2nd" in html and "out of 6" in html
    assert "230.0" in html and "76.67" in html
    assert [s.subject_name for s in entry["subjects"]] == [
        "Basic Science",
        "English Language",
        "Mathematics",
    ]
    for subject in ("Mathematics", "English Language", "Basic Science"):
        assert subject in html
    card_url = reverse("results:report_card", args=[st.pk, w.term.pk])
    assert card_url in html
    assert "Download report card" in html
    # Nobody else's data: other pupils' totals, averages and names are absent.
    for other in ("256.0", "85.33", "220.0", "73.33", "160.0", "119.0", "39.67"):
        assert other not in html
    for letter in "BCDEF":
        assert f"Pupil {letter}" not in html
        assert reverse("results:report_card", args=[w.students[letter].pk, w.term.pk]) not in html


def test_unpublished_term_hidden_while_another_is_published(client_a, w):
    """Term 1 published; a term 2 summary whose class is only approved stays hidden."""
    set_status(w, "published")
    st = w.students["A"]
    summary = ResultSummary.objects.get(student=st, term=w.term)
    ResultSummary.objects.create(
        student=st,
        term=w.term2,
        arm=w.arm,
        subjects_count=1,
        total="12.0",
        average="12.00",
        position=1,
        out_of=1,
        computed_at=summary.computed_at,
    )
    ClassResult.objects.create(arm=w.arm, term=w.term2, status="approved")
    r = page(client_a, st)
    assert [e["summary"].term_id for e in r.context["terms"]] == [w.term.pk]
    assert "12.00" not in r.content.decode()


def test_summary_from_another_class_needs_that_class_published(client_a, w):
    """Publication is checked for the summary's own arm, not any arm in the term."""
    set_status(w, "published")
    level = ClassLevel.objects.create(name="JSS 9", section="jss", order=99)
    arm2 = ClassArm.objects.create(level=level, suffix="Z")
    st = StudentProfileFactory()
    ResultSummary.objects.create(
        student=st,
        term=w.term,
        arm=arm2,
        subjects_count=1,
        total="12.0",
        average="12.00",
        position=1,
        out_of=1,
        computed_at=ResultSummary.objects.first().computed_at,
    )
    ClassResult.objects.create(arm=arm2, term=w.term, status="approved")
    r = page(client_a, st)
    assert r.context["terms"] == []


def test_staff_are_refused(client_a, w):
    client_a.force_login(w.ft)
    assert client_a.get(reverse("results:my_results")).status_code == 403
    client_a.force_login(w.proprietor)
    assert client_a.get(reverse("results:my_results")).status_code == 403


def test_anonymous_redirected_to_login(client_a, w):
    r = client_a.get(reverse("results:my_results"))
    assert r.status_code == 302


def test_query_count_does_not_grow_with_subjects(client_a, w, django_assert_max_num_queries):
    set_status(w, "published")
    st = w.students["A"]
    client_a.force_login(st.user)
    client_a.get(reverse("results:my_results"))  # warm the session
    with django_assert_max_num_queries(20), CaptureQueriesContext(connection) as small:
        r = client_a.get(reverse("results:my_results"))
    base = len(r.context["terms"][0]["subjects"])
    # More subject rows must not add queries.
    template = SubjectResult.objects.filter(student=st, term=w.term).first()
    for i in range(5):
        subject = Subject.objects.create(name=f"Extra {i}", code=f"x{i}", sections=["jss"])
        offering = SubjectOffering.objects.create(
            subject=subject, arm=w.arm, session=w.session, teacher=w.teacher
        )
        SubjectResult.objects.create(
            student=st,
            offering=offering,
            term=w.term,
            subject_name=subject.name,
            total=template.total,
            grade=template.grade,
            position=1,
            highest=template.highest,
            lowest=template.lowest,
            class_average=template.class_average,
        )
    with CaptureQueriesContext(connection) as large:
        r = client_a.get(reverse("results:my_results"))
    assert len(r.context["terms"][0]["subjects"]) == base + 5
    assert len(large) == len(small)


def test_home_links_to_my_results(client_a, w):
    client_a.force_login(w.students["A"].user)
    html = client_a.get(reverse("home")).content.decode()
    assert reverse("results:my_results") in html
    assert "My results" in html


def test_card_still_being_prepared(client_a, w):
    set_status(w, "published")
    html = page(client_a, w.students["A"]).content.decode()
    assert "being prepared" in html
    assert "Download report card" not in html
