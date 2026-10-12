import csv
import io
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, FormTeacher, Subject, SubjectOffering
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results import workflow
from apps.results.compute import compute_class
from apps.results.models import (
    AssessmentComponent,
    ClassResult,
    ElectiveChoice,
    GradeBand,
    ReportComment,
    ResultSummary,
    Score,
    ScoreSheet,
    SubjectResult,
)
from apps.results.scope import required_offerings
from apps.results.seeding import BASIC_SCALE
from apps.results.tasks import compute_class_task
from apps.results.workflow import (
    approve_class,
    publish_class,
    recall_sheet,
    review_class,
    submit_sheet,
    unlock_class,
)
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import make_sheet_world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def staff(*roles):
    user = StaffFactory()
    set_roles(user, list(roles), by=None)
    return user


def verified(user):
    """Service callers: mark the user as an OTP-verified session (as OTPMiddleware does)."""
    user.is_verified = lambda: True
    return user


def fill(offering, term, students, comps, teacher, value="5"):
    for st in students:
        for comp in comps:
            Score.objects.update_or_create(
                student=st,
                offering=offering,
                term=term,
                component=comp,
                defaults={"value": Decimal(value), "version": 1, "entered_by": teacher},
            )


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        world = make_sheet_world()
        GradeBand.objects.filter(section="jss").delete()
        for lower, upper, grade, remark in BASIC_SCALE:
            GradeBand.objects.create(
                section="jss", lower=lower, upper=upper, grade=grade, remark=remark
            )
        teacher2 = staff("teacher")
        extra = []
        for code, name in (("ENG", "English"), ("SCI", "Science")):
            subject, _ = Subject.objects.get_or_create(
                code=code, defaults={"name": name, "sections": ["jss"]}
            )
            extra.append(
                SubjectOffering.objects.create(
                    subject=subject, arm=world.arm, session=world.session, teacher=teacher2
                )
            )
        world.teacher2 = teacher2
        world.offerings = [world.offering, *extra]
        world.ft = staff("teacher")
        FormTeacher.objects.create(arm=world.arm, session=world.session, teacher=world.ft)
        world.principal = staff("principal")
        world.admin = verified(staff("school_admin"))
        world.proprietor = staff("proprietor")
        yield world


def teacher_of(w, offering):
    return w.teacher if offering.pk == w.offering.pk else w.teacher2


def fill_all(w):
    for off in w.offerings:
        fill(off, w.term, w.students, w.comps, teacher_of(w, off))


def comment_all(w):
    for st in w.students:
        ReportComment.objects.update_or_create(
            student=st, term=w.term, defaults={"class_teacher_comment": "Steady progress"}
        )


def submit_all(w):
    for off in w.offerings:
        submit_sheet(off, w.term, by=teacher_of(w, off))


def reviewed(w):
    fill_all(w)
    submit_all(w)
    comment_all(w)
    review_class(w.arm, w.term, by=w.ft)


def klass(w):
    return ClassResult.objects.get(arm=w.arm, term=w.term)


def sheet(w, offering):
    return ScoreSheet.objects.get(offering=offering, term=w.term)


def status_url(w):
    return reverse("results:class_status", args=[w.arm.pk])


def audit_payloads(action):
    return [e.after for e in AuditEvent.objects.filter(action=action).order_by("pk")]


# --- happy path --------------------------------------------------------------------------


def test_happy_path_through_views(client_a, w, settings, django_capture_on_commit_callbacks):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    fill_all(w)
    comment_all(w)

    client_a.force_login(w.teacher)
    r = client_a.post(reverse("results:sheet_submit", args=[w.offering.pk]))
    assert r.status_code == 302
    client_a.force_login(w.teacher2)
    for off in w.offerings[1:]:
        assert client_a.post(reverse("results:sheet_submit", args=[off.pk])).status_code == 302
    assert set(ScoreSheet.objects.values_list("status", flat=True)) == {"submitted"}
    assert all(s.first_submitted_at for s in ScoreSheet.objects.all())

    client_a.force_login(w.ft)
    r = client_a.post(reverse("results:class_review", args=[w.arm.pk]))
    assert r.status_code == 302, r.content
    assert klass(w).status == "reviewed"

    login_verified(client_a, w.principal)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(reverse("results:class_approve", args=[w.arm.pk]))
    assert r.status_code == 302
    k = klass(w)
    assert k.status == "approved" and k.compute_error == ""
    assert ResultSummary.objects.filter(arm=w.arm, term=w.term).count() == 3
    assert SubjectResult.objects.filter(term=w.term).count() == 9

    with (
        mock.patch("apps.results.workflow.on_class_published") as hook,
        django_capture_on_commit_callbacks(execute=True),
    ):
        r = client_a.post(reverse("results:class_publish", args=[w.arm.pk]))
    assert r.status_code == 302
    hook.assert_called_once()
    assert [a.pk for a in hook.call_args.args] == [w.arm.pk, w.term.pk]
    k = klass(w)
    assert k.status == "published" and k.published_by_id == w.principal.pk

    ids = {"arm_id": w.arm.pk, "term_id": w.term.pk}
    assert audit_payloads("results.class_reviewed") == [ids]
    assert audit_payloads("results.class_approved") == [ids]
    assert audit_payloads("results.class_published") == [ids]
    submitted = audit_payloads("results.sheet_submitted")
    assert len(submitted) == 3
    assert set(submitted[0]) == {"offering_id", "term_id", "sheet_id"}


def test_publish_by_school_admin(w, django_capture_on_commit_callbacks):
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    publish_class(w.arm, w.term, by=w.admin)
    assert klass(w).status == "published"


def test_status_page_shows_buttons_per_role(client_a, w):
    fill_all(w)
    client_a.force_login(w.ft)
    html = client_a.get(status_url(w)).content.decode()
    assert reverse("results:class_review", args=[w.arm.pk]) in html
    assert reverse("results:class_approve", args=[w.arm.pk]) not in html
    for off in w.offerings:
        assert off.subject.name in html
    assert "Draft" in html

    submit_all(w)
    comment_all(w)
    review_class(w.arm, w.term, by=w.ft)
    login_verified(client_a, w.principal)
    html = client_a.get(status_url(w)).content.decode()
    assert reverse("results:class_approve", args=[w.arm.pk]) in html
    assert reverse("results:class_review", args=[w.arm.pk]) not in html

    login_verified(client_a, w.admin)
    html = client_a.get(status_url(w)).content.decode()
    assert reverse("results:class_unlock", args=[w.arm.pk]) in html
    assert reverse("results:class_approve", args=[w.arm.pk]) not in html


def test_status_page_access(client_a, w):
    for user, verify in ((w.ft, False), (w.principal, True), (w.admin, True)):
        if verify:
            login_verified(client_a, user)
        else:
            client_a.force_login(user)
        assert client_a.get(status_url(w)).status_code == 200
    client_a.force_login(w.proprietor)
    assert client_a.get(status_url(w)).status_code == 200
    client_a.force_login(w.teacher)  # teaches the class but is not its form teacher
    assert client_a.get(status_url(w)).status_code == 403
    client_a.force_login(w.students[0].user)
    assert client_a.get(status_url(w)).status_code == 403
    client_a.force_login(w.proprietor)
    assert client_a.get(reverse("results:class_status", args=[999999])).status_code == 404


# --- submit and recall -------------------------------------------------------------------


def test_submit_with_blank_cells_reports_the_count(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    Score.objects.filter(student=w.students[0], component=w.comps[0]).update(value=None)
    Score.objects.filter(student=w.students[1], component=w.comps[1]).delete()
    with pytest.raises(ValidationError) as exc:
        submit_sheet(w.offering, w.term, by=w.teacher)
    message = " ".join(exc.value.messages)
    assert "2 score cells are blank" in message
    for st in w.students:
        assert st.first_name not in message and st.last_name not in message
    assert not ScoreSheet.objects.filter(status="submitted").exists()
    assert not AuditEvent.objects.filter(action="results.sheet_submitted").exists()


def test_submit_view_shows_blank_count_on_grid(client_a, w):
    client_a.force_login(w.teacher)
    r = client_a.post(reverse("results:sheet_submit", args=[w.offering.pk]), follow=True)
    assert "15 score cells are blank" in r.content.decode()


def test_withdrawn_student_does_not_block_submit(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    submit_sheet(w.offering, w.term, by=w.teacher)
    assert sheet(w, w.offering).status == "submitted"


def test_new_student_blocks_submit(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    StudentProfileFactory()
    with pytest.raises(ValidationError, match="5 score cells are blank"):
        submit_sheet(w.offering, w.term, by=w.teacher)


def test_submit_only_by_assigned_teacher(client_a, w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    for user in (w.teacher2, w.ft, w.principal):
        with pytest.raises(PermissionDenied):
            submit_sheet(w.offering, w.term, by=user)
    client_a.force_login(w.teacher2)
    assert client_a.post(reverse("results:sheet_submit", args=[w.offering.pk])).status_code == 403
    assert client_a.post(reverse("results:sheet_recall", args=[w.offering.pk])).status_code == 403
    assert ScoreSheet.objects.filter(status="submitted").count() == 0


def test_submit_needs_components_totalling_100(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    AssessmentComponent.objects.filter(pk=w.comps[0].pk).update(max_score=5)
    with pytest.raises(ValidationError, match="100"):
        submit_sheet(w.offering, w.term, by=w.teacher)


def test_submit_with_zero_takers_is_allowed(w):
    elective = w.offerings[2]
    SubjectOffering.objects.filter(pk=elective.pk).update(is_compulsory=False)
    elective.refresh_from_db()
    submit_sheet(elective, w.term, by=w.teacher2)
    assert sheet(w, elective).status == "submitted"


def test_submit_needs_draft_class_and_draft_sheet(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    submit_sheet(w.offering, w.term, by=w.teacher)
    with pytest.raises(ValidationError):
        submit_sheet(w.offering, w.term, by=w.teacher)
    recall_sheet(w.offering, w.term, by=w.teacher)
    ClassResult.objects.filter(arm=w.arm, term=w.term).update(status="reviewed")
    with pytest.raises(ValidationError):
        submit_sheet(w.offering, w.term, by=w.teacher)


def test_first_submitted_at_set_only_once(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    submit_sheet(w.offering, w.term, by=w.teacher)
    first = sheet(w, w.offering).first_submitted_at
    assert first is not None
    recall_sheet(w.offering, w.term, by=w.teacher)
    s = sheet(w, w.offering)
    assert s.status == "draft" and s.first_submitted_at == first
    submit_sheet(w.offering, w.term, by=w.teacher)
    s = sheet(w, w.offering)
    assert s.first_submitted_at == first and s.submitted_at > first
    assert audit_payloads("results.sheet_recalled") == [
        {"offering_id": w.offering.pk, "term_id": w.term.pk, "sheet_id": s.pk}
    ]


def test_recall_after_review_refused(client_a, w):
    reviewed(w)
    with pytest.raises(ValidationError):
        recall_sheet(w.offering, w.term, by=w.teacher)
    client_a.force_login(w.teacher)
    r = client_a.post(reverse("results:sheet_recall", args=[w.offering.pk]), follow=True)
    assert "moved on" in r.content.decode()
    assert sheet(w, w.offering).status == "submitted"
    assert klass(w).status == "reviewed"


def test_recall_needs_submitted_sheet(w):
    with pytest.raises(ValidationError):
        recall_sheet(w.offering, w.term, by=w.teacher)


def test_grid_shows_submit_and_recall_buttons(client_a, w):
    client_a.force_login(w.teacher)
    html = client_a.get(reverse("results:grid", args=[w.offering.pk])).content.decode()
    assert reverse("results:sheet_submit", args=[w.offering.pk]) in html
    assert reverse("results:sheet_recall", args=[w.offering.pk]) not in html
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    submit_sheet(w.offering, w.term, by=w.teacher)
    html = client_a.get(reverse("results:grid", args=[w.offering.pk])).content.decode()
    assert reverse("results:sheet_submit", args=[w.offering.pk]) not in html
    assert reverse("results:sheet_recall", args=[w.offering.pk]) in html


def test_submit_and_recall_lock_order(w):
    fill(w.offering, w.term, w.students, w.comps, w.teacher)
    names = []
    with mock.patch("apps.results.workflow.school_lock", side_effect=names.append):
        submit_sheet(w.offering, w.term, by=w.teacher)
        recall_sheet(w.offering, w.term, by=w.teacher)
    scores_key = f"scores:{w.offering.pk}:{w.term.pk}"
    results_key = f"results:{w.arm.pk}:{w.term.pk}"
    assert names == [scores_key, results_key, scores_key, results_key]


# --- review ------------------------------------------------------------------------------


def test_review_by_non_form_teacher_is_403(client_a, w):
    fill_all(w)
    submit_all(w)
    comment_all(w)
    for user in (w.teacher, w.principal):
        with pytest.raises(PermissionDenied):
            review_class(w.arm, w.term, by=user)
    client_a.force_login(w.teacher)
    assert client_a.post(reverse("results:class_review", args=[w.arm.pk])).status_code == 403
    assert klass(w).status == "draft"


def test_review_needs_every_sheet_submitted(client_a, w):
    fill_all(w)
    comment_all(w)
    submit_sheet(w.offering, w.term, by=w.teacher)
    with pytest.raises(ValidationError, match="2 of 3"):
        review_class(w.arm, w.term, by=w.ft)
    client_a.force_login(w.ft)
    r = client_a.post(reverse("results:class_review", args=[w.arm.pk]))
    assert r.status_code == 400 and "2 of 3" in r.content.decode()
    assert klass(w).status == "draft"


def test_review_needs_class_teacher_comment_for_every_active_student(w):
    fill_all(w)
    submit_all(w)
    comment_all(w)
    ReportComment.objects.filter(student=w.students[0]).update(class_teacher_comment="  ")
    with pytest.raises(ValidationError, match="1 student"):
        review_class(w.arm, w.term, by=w.ft)
    ReportComment.objects.filter(student=w.students[0]).update(class_teacher_comment="Fine")
    review_class(w.arm, w.term, by=w.ft)  # the withdrawn student needs no comment
    assert klass(w).status == "reviewed"


def test_review_twice_is_a_state_error(client_a, w):
    reviewed(w)
    with pytest.raises(ValidationError):
        review_class(w.arm, w.term, by=w.ft)
    client_a.force_login(w.ft)
    assert client_a.post(reverse("results:class_review", args=[w.arm.pk])).status_code == 409


# --- required offerings ------------------------------------------------------------------


def make_elective(w, takers):
    elective = w.offerings[2]
    SubjectOffering.objects.filter(pk=elective.pk).update(is_compulsory=False)
    elective.refresh_from_db()
    for st in takers:
        ElectiveChoice.objects.create(student=st, offering=elective)
    return elective


def test_elective_with_no_takers_blocks_nothing(w, django_capture_on_commit_callbacks):
    elective = make_elective(w, [])
    elective_withdrawn = make_elective(w, [w.withdrawn])  # a withdrawn taker is not ranked
    assert elective_withdrawn.pk == elective.pk
    assert set(required_offerings(w.arm, w.term)) == set(w.offerings[:2])
    for off in w.offerings[:2]:
        fill(off, w.term, w.students, w.comps, teacher_of(w, off))
        submit_sheet(off, w.term, by=teacher_of(w, off))
    comment_all(w)
    review_class(w.arm, w.term, by=w.ft)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    assert klass(w).status == "approved"
    assert not SubjectResult.objects.filter(offering=elective).exists()
    assert {s.subjects_count for s in ResultSummary.objects.filter(term=w.term)} == {2}


def test_elective_with_unsubmitted_takers_blocks_review_and_compute(w):
    elective = make_elective(w, [w.students[0]])
    assert set(required_offerings(w.arm, w.term)) == set(w.offerings)
    for off in w.offerings[:2]:
        fill(off, w.term, w.students, w.comps, teacher_of(w, off))
        submit_sheet(off, w.term, by=teacher_of(w, off))
    fill(elective, w.term, [w.students[0]], w.comps, w.teacher2)
    comment_all(w)
    with pytest.raises(ValidationError, match="1 of 3"):
        review_class(w.arm, w.term, by=w.ft)
    with pytest.raises(ValidationError, match=str(elective.pk)):
        compute_class(w.arm, w.term)
    submit_sheet(elective, w.term, by=w.teacher2)
    review_class(w.arm, w.term, by=w.ft)
    compute_class(w.arm, w.term)
    assert SubjectResult.objects.filter(offering=elective).count() == 1


# --- approve and compute -----------------------------------------------------------------


def test_approve_by_school_admin_not_principal_is_403(client_a, w):
    reviewed(w)
    with pytest.raises(PermissionDenied):
        approve_class(w.arm, w.term, by=w.admin)
    login_verified(client_a, w.admin)
    assert client_a.post(reverse("results:class_approve", args=[w.arm.pk])).status_code == 403
    assert klass(w).status == "reviewed"


def test_approve_needs_reviewed(w):
    with pytest.raises(ValidationError):
        approve_class(w.arm, w.term, by=w.principal)
    assert not ClassResult.objects.exclude(status="draft").exists()


def test_approve_enqueues_compute_on_commit_on_default_queue(w, django_capture_on_commit_callbacks):
    reviewed(w)
    assert compute_class_task.queue == "default"
    with mock.patch("apps.results.tasks.compute_class_task.apply_async") as enqueue:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            approve_class(w.arm, w.term, by=w.principal)
        enqueue.assert_not_called()
        assert len(callbacks) == 1
        callbacks[0]()
    enqueue.assert_called_once()
    assert list(enqueue.call_args.kwargs["args"]) == [w.arm.pk, w.term.pk]


def test_compute_failure_returns_class_to_reviewed(client_a, w, django_capture_on_commit_callbacks):
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
        # A cell goes blank before the compute task runs.
        Score.objects.filter(student=w.students[0], offering=w.offering).update(value=None)
    k = klass(w)
    assert k.status == "reviewed"
    assert k.compute_error and "blank" in k.compute_error
    for st in w.students:
        assert st.last_name not in k.compute_error
    assert not ResultSummary.objects.exists()
    login_verified(client_a, w.principal)
    html = client_a.get(status_url(w)).content.decode()
    assert k.compute_error in html


def test_compute_task_ignores_class_no_longer_approved(w):
    reviewed(w)
    compute_class_task.delay(w.arm.pk, w.term.pk)
    assert klass(w).status == "reviewed"
    assert not ResultSummary.objects.exists()


# --- publish -----------------------------------------------------------------------------


def test_publish_without_summaries_is_refused(client_a, w):
    reviewed(w)
    approve_class(w.arm, w.term, by=w.principal)  # on_commit never runs: nothing computed
    with pytest.raises(ValidationError, match="computed"):
        publish_class(w.arm, w.term, by=w.principal)
    login_verified(client_a, w.principal)
    r = client_a.post(reverse("results:class_publish", args=[w.arm.pk]))
    assert r.status_code in (400, 409) and "computed" in r.content.decode()
    assert klass(w).status == "approved"


def test_publish_needs_fresh_summaries(w, django_capture_on_commit_callbacks):
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    ResultSummary.objects.update(computed_at=timezone.now() - timedelta(days=1))
    with pytest.raises(ValidationError):
        publish_class(w.arm, w.term, by=w.principal)
    ResultSummary.objects.update(computed_at=timezone.now())
    ClassResult.objects.filter(pk=klass(w).pk).update(compute_error="x")
    with pytest.raises(ValidationError):
        publish_class(w.arm, w.term, by=w.principal)


def test_publish_permissions_and_state(w):
    reviewed(w)
    with pytest.raises(ValidationError):
        publish_class(w.arm, w.term, by=w.principal)  # still reviewed
    with pytest.raises(PermissionDenied):
        publish_class(w.arm, w.term, by=w.ft)
    with pytest.raises(PermissionDenied):
        publish_class(w.arm, w.term, by=w.proprietor)


# --- after publish: edits refused, then unlock -------------------------------------------


def published(w, captured):
    reviewed(w)
    with captured(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    publish_class(w.arm, w.term, by=w.principal)
    assert klass(w).status == "published"


def test_edits_after_publish_refused_then_unlock(client_a, w, django_capture_on_commit_callbacks):
    published(w, django_capture_on_commit_callbacks)
    client_a.force_login(w.teacher)
    s0, c0 = w.students[0], w.comps[0]
    cell = Score.objects.get(student=s0, offering=w.offering, component=c0)
    put_url = f"/api/v1/scores?offering={w.offering.pk}&term={w.term.pk}"
    body = {"cells": [{"student": s0.pk, "component": c0.pk, "value": "9", "version": 1}]}

    r = client_a.put(put_url, data=json.dumps(body), content_type="application/json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESULTS_LOCKED"
    assert AuditEvent.objects.filter(action="results.edit_refused").count() == 1

    out = io.StringIO()
    rows = list(
        csv.reader(
            io.StringIO(
                client_a.get(reverse("results:grid_csv", args=[w.offering.pk])).content.decode()
            )
        )
    )
    rows[1][2] = "9"
    csv.writer(out).writerows(rows)
    r = client_a.post(
        reverse("results:grid_upload", args=[w.offering.pk]),
        {"file": SimpleUploadedFile("s.csv", out.getvalue().encode(), content_type="text/csv")},
    )
    assert r.status_code == 409 and "locked" in r.content.decode()
    assert AuditEvent.objects.filter(action="results.edit_refused").count() == 2
    cell.refresh_from_db()
    assert cell.value == Decimal("5")

    login_verified(client_a, w.admin)
    with (
        mock.patch("apps.results.workflow.on_class_unlocked") as hook,
        django_capture_on_commit_callbacks(execute=True),
    ):
        r = client_a.post(
            reverse("results:class_unlock", args=[w.arm.pk]),
            {"reason": "  Maths CA1 was mis-keyed  ", "sheets": [str(sheet(w, w.offering).pk)]},
        )
    assert r.status_code == 302, r.content
    hook.assert_called_once()
    assert klass(w).status == "draft"
    assert sheet(w, w.offering).status == "draft"
    assert sheet(w, w.offerings[1]).status == "submitted"
    assert not ResultSummary.objects.filter(arm=w.arm, term=w.term).exists()
    assert not SubjectResult.objects.filter(term=w.term).exists()
    assert audit_payloads("results.unlocked") == [
        {
            "arm_id": w.arm.pk,
            "term_id": w.term.pk,
            "sheet_ids": [sheet(w, w.offering).pk],
            "reason": "Maths CA1 was mis-keyed",
        }
    ]

    client_a.force_login(w.teacher)
    r = client_a.put(put_url, data=json.dumps(body), content_type="application/json")
    assert r.status_code == 200, r.content
    cell.refresh_from_db()
    assert cell.value == Decimal("9")
    changed = AuditEvent.objects.get(action="results.score_changed")
    assert changed.after == {"student_id": s0.pk, "component_id": c0.pk, "value": "9.0"}


def test_unlock_requires_reason(client_a, w, django_capture_on_commit_callbacks):
    published(w, django_capture_on_commit_callbacks)
    for reason in ("", "   ", "x" * 301):
        with pytest.raises(ValidationError):
            unlock_class(w.arm, w.term, sheets=[], reason=reason, by=w.admin)
    unlock_class(w.arm, w.term, sheets=[], reason="x" * 300, by=w.admin)
    assert klass(w).status == "draft"


def test_unlock_view_without_reason_shows_error(client_a, w, django_capture_on_commit_callbacks):
    published(w, django_capture_on_commit_callbacks)
    login_verified(client_a, w.admin)
    r = client_a.post(reverse("results:class_unlock", args=[w.arm.pk]), {"reason": " "})
    assert r.status_code == 400 and "reason" in r.content.decode()
    assert klass(w).status == "published"
    for bad in ("abc", "1.5", "-1"):
        r = client_a.post(
            reverse("results:class_unlock", args=[w.arm.pk]), {"reason": "ok", "sheets": [bad]}
        )
        assert r.status_code == 400
    assert klass(w).status == "published"


def test_unlock_sheet_must_belong_to_arm_and_term(w, django_capture_on_commit_callbacks):
    published(w, django_capture_on_commit_callbacks)
    other_term = ScoreSheet.objects.create(offering=w.offering, term=w.term2)
    level = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
    arm2 = ClassArm.objects.create(level=level, suffix="B")
    off2 = SubjectOffering.objects.create(
        subject=w.offering.subject, arm=arm2, session=w.session, teacher=w.teacher
    )
    other_arm = ScoreSheet.objects.create(offering=off2, term=w.term)
    for bad in (other_term, other_arm):
        with pytest.raises(ValidationError):
            unlock_class(w.arm, w.term, sheets=[bad], reason="fix", by=w.admin)
    assert klass(w).status == "published"


def test_unlock_only_by_verified_school_admin(client_a, w, django_capture_on_commit_callbacks):
    published(w, django_capture_on_commit_callbacks)
    for user in (w.principal, w.proprietor, w.ft, staff("school_admin")):  # last: unverified
        with pytest.raises(PermissionDenied):
            unlock_class(w.arm, w.term, sheets=[], reason="fix", by=user)
    login_verified(client_a, w.principal)
    r = client_a.post(reverse("results:class_unlock", args=[w.arm.pk]), {"reason": "fix"})
    assert r.status_code == 403
    assert klass(w).status == "published"


def test_unlock_lock_order(w, django_capture_on_commit_callbacks):
    published(w, django_capture_on_commit_callbacks)
    chosen = sorted(
        [sheet(w, w.offerings[2]), sheet(w, w.offerings[0])], key=lambda s: -s.offering_id
    )
    names = []
    with mock.patch("apps.results.workflow.school_lock", side_effect=names.append):
        unlock_class(w.arm, w.term, sheets=chosen, reason="fix", by=w.admin)
    offs = sorted(s.offering_id for s in chosen)
    assert names == [
        f"scores:{offs[0]}:{w.term.pk}",
        f"scores:{offs[1]}:{w.term.pk}",
        f"results:{w.arm.pk}:{w.term.pk}",
    ]


def test_unlock_draft_class_without_sheets_is_a_state_error(w):
    with pytest.raises(ValidationError):
        unlock_class(w.arm, w.term, sheets=[], reason="fix", by=w.admin)


def test_hooks_delegate_to_report_cards():
    """Publishing queues report cards and notifications; unlocking removes the cards."""
    arm, term = SimpleNamespace(pk=1), SimpleNamespace(pk=2)
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async") as enqueue,
        mock.patch("apps.results.tasks.notify_published_task.apply_async") as notify,
    ):
        assert workflow.on_class_published(arm, term) is None
    enqueue.assert_called_once_with(args=[1, 2], queue="reports")
    notify.assert_called_once_with(args=[1, 2], queue="notifications")
    with mock.patch("apps.results.cards.remove_report_cards") as remove:
        assert workflow.on_class_unlocked(arm, term) is None
    remove.assert_called_once_with(arm, term)


def test_hooks_not_called_without_commit(w, django_capture_on_commit_callbacks):
    published_hook = mock.patch("apps.results.workflow.on_class_published")
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    with published_hook as hook, django_capture_on_commit_callbacks(execute=False) as cbs:
        publish_class(w.arm, w.term, by=w.principal)
        hook.assert_not_called()
    assert len(cbs) == 1


def test_actions_use_current_term_and_need_one(client_a, w):
    from apps.academics.models import Term

    Term.objects.update(is_current=False)
    client_a.force_login(w.ft)
    r = client_a.get(status_url(w))
    assert r.status_code == 200 and "Set a current term first" in r.content.decode()
    r = client_a.post(reverse("results:class_review", args=[w.arm.pk]))
    assert r.status_code == 409


def test_publish_freshness_uses_database_time(w, django_capture_on_commit_callbacks):
    """approved_at and computed_at both come from the database clock, so a web host whose
    clock runs ahead of the worker's cannot block publishing (final review M1)."""
    reviewed(w)
    ahead = timezone.now() + timedelta(hours=1)
    with (
        mock.patch("apps.results.workflow.timezone.now", return_value=ahead),
        django_capture_on_commit_callbacks(execute=True),
    ):
        approve_class(w.arm, w.term, by=w.principal)
    k = klass(w)
    assert k.approved_at < ahead - timedelta(minutes=30)
    computed = ResultSummary.objects.filter(arm=w.arm, term=w.term).first().computed_at
    assert computed >= k.approved_at
    publish_class(w.arm, w.term, by=w.principal)
    assert klass(w).status == "published"


# --- recovery: queueing failures, retry compute, regenerate cards (final review I3) -------


class BrokerDown(Exception):
    pass


def test_broker_failure_on_approve_shows_error_not_500(
    client_a, w, django_capture_on_commit_callbacks, caplog
):
    reviewed(w)
    login_verified(client_a, w.principal)
    with (
        mock.patch(
            "apps.results.tasks.compute_class_task.apply_async",
            side_effect=BrokerDown("amqp://user:secret@broker"),
        ),
        caplog.at_level("ERROR", logger="apps.results.workflow"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        r = client_a.post(reverse("results:class_approve", args=[w.arm.pk]))
    assert r.status_code == 302
    k = klass(w)
    assert k.status == "approved"
    assert k.compute_error == ClassResult.START_ERROR
    assert "BrokerDown" in caplog.text and "secret" not in caplog.text
    html = client_a.get(status_url(w)).content.decode()
    assert "Could not start — please retry" in html
    assert reverse("results:class_retry_compute", args=[w.arm.pk]) in html
    with pytest.raises(ValidationError, match="Retry"):
        publish_class(w.arm, w.term, by=w.principal)


def test_broker_failure_on_publish_sets_cards_error(w, django_capture_on_commit_callbacks):
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    with (
        mock.patch(
            "apps.results.tasks.generate_report_cards_task.apply_async", side_effect=BrokerDown
        ),
        mock.patch("apps.results.tasks.notify_published_task.apply_async") as notify,
        django_capture_on_commit_callbacks(execute=True),
    ):
        publish_class(w.arm, w.term, by=w.principal)
    notify.assert_called_once()  # one failed enqueue does not stop the other
    k = klass(w)
    assert k.status == "published" and k.cards_error == ClassResult.START_ERROR


def test_unexpected_compute_error_keeps_class_approved_with_generic_error(
    w, django_capture_on_commit_callbacks, caplog
):
    reviewed(w)
    with (
        mock.patch(
            "apps.results.workflow.compute_class",
            side_effect=RuntimeError(f"bad row for {w.students[0].last_name}"),
        ),
        caplog.at_level("ERROR", logger="apps.results.workflow"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        approve_class(w.arm, w.term, by=w.principal)
    k = klass(w)
    assert k.status == "approved"
    assert k.compute_error == ClassResult.COMPUTE_FAILED
    assert not ResultSummary.objects.exists()
    assert "RuntimeError" in caplog.text
    assert w.students[0].last_name not in caplog.text
    assert any(rec.exc_info for rec in caplog.records)  # the traceback is kept


def test_retry_compute_succeeds(client_a, w, django_capture_on_commit_callbacks):
    reviewed(w)
    with (
        mock.patch("apps.results.workflow.compute_class", side_effect=RuntimeError),
        django_capture_on_commit_callbacks(execute=True),
    ):
        approve_class(w.arm, w.term, by=w.principal)
    assert klass(w).compute_error == ClassResult.COMPUTE_FAILED

    login_verified(client_a, w.admin)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(reverse("results:class_retry_compute", args=[w.arm.pk]))
    assert r.status_code == 302, r.content
    k = klass(w)
    assert k.status == "approved" and k.compute_error == ""
    assert ResultSummary.objects.filter(arm=w.arm, term=w.term).count() == 3
    assert audit_payloads("results.compute_retried") == [{"arm_id": w.arm.pk, "term_id": w.term.pk}]
    html = client_a.get(status_url(w)).content.decode()
    assert reverse("results:class_retry_compute", args=[w.arm.pk]) not in html
    publish_class(w.arm, w.term, by=w.admin)
    assert klass(w).status == "published"


def test_retry_compute_when_computation_never_ran(w, django_capture_on_commit_callbacks):
    reviewed(w)
    approve_class(w.arm, w.term, by=w.principal)  # on_commit never runs: stale
    with django_capture_on_commit_callbacks(execute=True):
        workflow.retry_compute(w.arm, w.term, by=w.principal)
    assert ResultSummary.objects.filter(arm=w.arm, term=w.term).count() == 3


def test_retry_compute_refusals(client_a, w, django_capture_on_commit_callbacks):
    with pytest.raises(workflow.ResultsStateError):
        workflow.retry_compute(w.arm, w.term, by=w.principal)  # draft
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    with pytest.raises(workflow.ResultsStateError, match="already computed"):
        workflow.retry_compute(w.arm, w.term, by=w.principal)
    login_verified(client_a, w.principal)
    r = client_a.post(reverse("results:class_retry_compute", args=[w.arm.pk]))
    assert r.status_code == 409 and "already computed" in r.content.decode()
    for user in (w.proprietor, w.ft):
        with pytest.raises(PermissionDenied):
            workflow.retry_compute(w.arm, w.term, by=user)
    client_a.force_login(w.proprietor)
    assert client_a.post(reverse("results:class_retry_compute", args=[w.arm.pk])).status_code == 403
    client_a.force_login(w.ft)
    assert client_a.post(reverse("results:class_retry_compute", args=[w.arm.pk])).status_code == 403
    assert client_a.get(reverse("results:class_retry_compute", args=[w.arm.pk])).status_code == 405
    assert not AuditEvent.objects.filter(action="results.compute_retried").exists()


def _published_quietly(w, captured):
    reviewed(w)
    with captured(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    publish_class(w.arm, w.term, by=w.principal)  # on_commit not run: nothing queued


def test_regenerate_report_cards(client_a, w, django_capture_on_commit_callbacks):
    _published_quietly(w, django_capture_on_commit_callbacks)
    ClassResult.objects.filter(arm=w.arm, term=w.term).update(cards_error=ClassResult.START_ERROR)
    login_verified(client_a, w.principal)
    html = client_a.get(status_url(w)).content.decode()
    assert reverse("results:class_regenerate_cards", args=[w.arm.pk]) in html
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async") as enqueue,
        mock.patch("apps.results.tasks.notify_published_task.apply_async") as notify,
        django_capture_on_commit_callbacks(execute=True),
    ):
        r = client_a.post(reverse("results:class_regenerate_cards", args=[w.arm.pk]))
    assert r.status_code == 302, r.content
    enqueue.assert_called_once_with(args=[w.arm.pk, w.term.pk], queue="reports")
    notify.assert_not_called()  # guardians were already told at publish
    assert klass(w).cards_error == ""
    assert audit_payloads("results.report_cards_regenerated") == [
        {"arm_id": w.arm.pk, "term_id": w.term.pk, "notify": False}
    ]


def test_regenerate_also_queues_notifications_that_could_not_start(
    w, django_capture_on_commit_callbacks
):
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async"),
        mock.patch("apps.results.tasks.notify_published_task.apply_async", side_effect=OSError),
        django_capture_on_commit_callbacks(execute=True),
    ):
        publish_class(w.arm, w.term, by=w.principal)
    assert klass(w).cards_error == ClassResult.NOTIFY_START_ERROR
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async") as enqueue,
        mock.patch("apps.results.tasks.notify_published_task.apply_async") as notify,
        django_capture_on_commit_callbacks(execute=True),
    ):
        workflow.regenerate_report_cards(w.arm, w.term, by=w.admin)
    enqueue.assert_called_once()
    notify.assert_called_once_with(args=[w.arm.pk, w.term.pk], queue="notifications")
    assert klass(w).cards_error == ""


def test_regenerate_refusals(client_a, w):
    reviewed(w)
    with pytest.raises(workflow.ResultsStateError):
        workflow.regenerate_report_cards(w.arm, w.term, by=w.principal)
    login_verified(client_a, w.principal)
    r = client_a.post(reverse("results:class_regenerate_cards", args=[w.arm.pk]))
    assert r.status_code == 409
    for user in (w.proprietor, w.ft):
        with pytest.raises(PermissionDenied):
            workflow.regenerate_report_cards(w.arm, w.term, by=user)
    client_a.force_login(w.proprietor)
    r = client_a.post(reverse("results:class_regenerate_cards", args=[w.arm.pk]))
    assert r.status_code == 403
    assert not AuditEvent.objects.filter(action="results.report_cards_regenerated").exists()


# --- navigation (final review I2, M12) ----------------------------------------------------


def arm_detail_url(w):
    return reverse("academics:arm_detail", args=[w.arm.pk])


def test_arm_detail_links_to_class_status_for_managers(client_a, w):
    status = status_url(w)
    comments = reverse("results:class_comments", args=[w.arm.pk])
    for user, verify, sees_comments in (
        (w.principal, True, True),
        (w.admin, True, False),
        (w.proprietor, False, False),
    ):
        if verify:
            login_verified(client_a, user)
        else:
            client_a.force_login(user)
        r = client_a.get(arm_detail_url(w))
        assert r.status_code == 200
        html = r.content.decode()
        assert status in html
        assert (comments in html) is sees_comments
        assert client_a.get(status).status_code == 200
        assert (client_a.get(comments).status_code == 200) is sees_comments


def test_class_status_shows_comments_link_only_to_those_who_can_open_it(client_a, w):
    comments = reverse("results:class_comments", args=[w.arm.pk])
    client_a.force_login(w.ft)
    assert comments in client_a.get(status_url(w)).content.decode()
    login_verified(client_a, w.principal)
    assert comments in client_a.get(status_url(w)).content.decode()
    login_verified(client_a, w.admin)
    assert comments not in client_a.get(status_url(w)).content.decode()
    assert client_a.get(comments).status_code == 403


def test_published_class_lists_report_cards_for_staff(
    client_a, w, django_capture_on_commit_callbacks
):
    from apps.results.models import ReportCard

    _published_quietly(w, django_capture_on_commit_callbacks)
    ranked = list(w.students)
    ready = ranked[0]
    ReportCard.objects.create(
        student=ready,
        term=w.term,
        arm=w.arm,
        file_name="report_cards/x.pdf",
        generated_at=timezone.now(),
    )
    for user, verify in ((w.principal, True), (w.ft, False), (w.proprietor, False)):
        if verify:
            login_verified(client_a, user)
        else:
            client_a.force_login(user)
        html = client_a.get(status_url(w)).content.decode()
        assert reverse("results:report_card", args=[ready.pk, w.term.pk]) in html
        for st in ranked[1:]:
            assert reverse("results:report_card", args=[st.pk, w.term.pk]) not in html
            assert st.full_name in html
        assert html.count("not generated yet") == len(ranked) - 1


def test_unpublished_class_lists_no_report_cards(client_a, w, django_capture_on_commit_callbacks):
    reviewed(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_class(w.arm, w.term, by=w.principal)
    login_verified(client_a, w.principal)
    html = client_a.get(status_url(w)).content.decode()
    assert "Report cards" not in html and "not generated yet" not in html


def test_unlock_form_asks_for_no_personal_details(client_a, w, django_capture_on_commit_callbacks):
    _published_quietly(w, django_capture_on_commit_callbacks)
    login_verified(client_a, w.admin)
    html = client_a.get(status_url(w)).content.decode()
    assert "Do not include student names or personal details." in html
    assert 'aria-describedby="reason-help"' in html
