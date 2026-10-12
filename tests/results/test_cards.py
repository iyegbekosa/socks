"""Report-card PDFs (FR-09): contents, storage, hooks and secure downloads."""

import io
import logging
import re
import time
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import cloudinary
import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, FormTeacher, Subject, SubjectOffering
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.branding.models import Branding
from apps.results import cards
from apps.results.cards import card_context, generate_report_cards, render_card
from apps.results.compute import compute_class
from apps.results.models import (
    AssessmentComponent,
    ClassResult,
    Rating,
    ReportCard,
    ReportComment,
    ResultSummary,
    Score,
    ScoreSheet,
    Trait,
)
from apps.results.workflow import publish_class, unlock_class
from apps.tenancy.limits import PlanLimitExceeded
from tests.accounts.factories import StaffFactory
from tests.results.factories import make_card, make_sheet_world
from tests.results.test_accuracy import (
    SCORES,
    SUBJECTS,
    _enter,
    _jss_structure,
    _offerings,
    _submit,
    _term,
)
from tests.students.factories import StudentProfileFactory, default_arm, default_session

pytestmark = [pytest.mark.django_db, pytest.mark.pdf]

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99="
    b"\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)
PRINCIPAL_COMMENT = "A fine term <b>overall</b> & well done"


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return " ".join(" ".join((page.extract_text() or "") for page in reader.pages).split())


def staff(*roles):
    user = StaffFactory()
    set_roles(user, list(roles), by=None)
    return user


class Uploads:
    """Captures Cloudinary uploads (public_id -> bytes) and destroys."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.destroyed: list[str] = []

    def upload(self, data, **kwargs):
        self.files[kwargs["public_id"]] = data
        return {"public_id": kwargs["public_id"]}

    def destroy(self, public_id, **kwargs):
        self.destroyed.append(public_id)
        return {"result": "ok"}

    def pdf_for(self, student, term) -> bytes:
        key = f"dev/school_a/report_cards/{term.session_id}/{term.pk}/{student.pk}.pdf"
        return self.files[key]


@pytest.fixture
def uploads():
    u = Uploads()
    with (
        mock.patch("cloudinary.uploader.upload", side_effect=u.upload),
        mock.patch("cloudinary.uploader.destroy", side_effect=u.destroy),
    ):
        yield u


@pytest.fixture
def logo_open():
    """The school logo as the public storage returns it."""
    with mock.patch(
        "apps.core.storage.TenantCloudinaryStorage._open",
        side_effect=lambda name, mode="rb": ContentFile(PNG, name=name),
    ) as m:
        yield m


def _branding():
    b, _ = Branding.objects.get_or_create(pk=1)
    b.motto = "Knowledge and Light"
    b.address = "12 Allen Avenue, Ikeja"
    b.primary_color = "#123456"
    b.logo.name = "branding/logo.png"
    b.save()


def _accuracy_class(term_number: int = 1) -> SimpleNamespace:
    """The Task 7 accuracy dataset in ``term_number``, computed, with comments and ratings."""
    from apps.academics.models import Term

    Term.objects.filter(is_current=True).update(is_current=False)
    session = default_session()
    terms = {n: _term(session, n, current=(n == term_number)) for n in (1, 2, 3)}
    term = terms[term_number]
    arm = default_arm()
    comps = list(_jss_structure())
    teacher = StaffFactory(first_name="Ada", last_name="Okafor")
    offerings = _offerings(arm, session, teacher, SUBJECTS)
    students = {}
    for letter, rows in SCORES.items():
        st = StudentProfileFactory(first_name="Pupil", last_name=letter)
        students[letter] = st
        for name, values in zip(SUBJECTS, rows, strict=True):
            _enter(st, offerings[name], term, comps, values, teacher)
    _submit(offerings.values(), term)
    if term_number == 3:
        # Pupil B: 60 in the 1st term and 70 in the 2nd term for every subject.
        for name in SUBJECTS:
            _enter(students["B"], offerings[name], terms[1], comps, (20, 40), teacher)
            _enter(students["B"], offerings[name], terms[2], comps, (30, 40), teacher)
    compute_class(arm, term)
    for letter, st in students.items():
        ReportComment.objects.create(
            student=st,
            term=term,
            class_teacher_comment=f"Pupil {letter} works hard",
            principal_comment=PRINCIPAL_COMMENT,
            days_present=58,
            days_open=60,
        )
    trait_a = Trait.objects.get_or_create(kind="affective", name="Punctuality")[0]
    trait_p = Trait.objects.get_or_create(kind="psychomotor", name="Handwriting")[0]
    Rating.objects.create(student=students["B"], term=term, trait=trait_a, value=4)
    Rating.objects.create(student=students["B"], term=term, trait=trait_p, value=3)
    _branding()
    return SimpleNamespace(
        session=session,
        terms=terms,
        term=term,
        arm=arm,
        comps=comps,
        teacher=teacher,
        offerings=offerings,
        students=students,
    )


def _publish(w):
    ClassResult.objects.update_or_create(
        arm=w.arm, term=w.term, defaults={"status": ClassResult.Status.PUBLISHED}
    )


@pytest.fixture
def acc(school_a, logo_open):
    with schema_context("school_a"):
        yield _accuracy_class()


# --- contents ------------------------------------------------------------------------------


def test_card_text_for_accuracy_student(acc, uploads):
    _publish(acc)
    assert generate_report_cards(acc.arm, acc.term) == 6
    b = acc.students["B"]
    text = pdf_text(uploads.pdf_for(b, acc.term))

    assert "School A" in text
    assert b.full_name in text
    assert b.admission_number in text
    assert acc.arm.display_name in text
    assert "2026/2027" in text and "1st Term" in text
    assert "Knowledge and Light" in text and "12 Allen Avenue, Ikeja" in text
    # Subject positions 2nd, 2nd, 4th; class position 3rd out of 6 (tied with C).
    assert re.search(r"Mathematics 35\.0 45\.0 80\.0 A Excellent 2nd 98\.0 30\.0 67\.17", text)
    assert re.search(r"Basic Science 30\.0 40\.0 70\.0 A Excellent 4th", text)
    assert "3rd out of 6" in text
    assert "220.0" in text and "73.33" in text
    assert "CA (40)" in text and "Exam (60)" in text
    assert "58 / 60" in text
    assert "Punctuality" in text and "Handwriting" in text
    assert "Pupil B works hard" in text
    assert PRINCIPAL_COMMENT in text  # autoescaped: shown as typed, not as markup
    assert "5 January 2027" in text  # next term begins
    assert "70 – 100" in text and "Very good" in text  # grading key


def test_each_grade_on_the_card(acc, uploads):
    _publish(acc)
    generate_report_cards(acc.arm, acc.term)
    c = pdf_text(uploads.pdf_for(acc.students["C"], acc.term))
    assert re.search(r"Mathematics 20\.0 40\.0 60\.0 B Very good 4th", c)
    assert re.search(r"English Language 35\.0 50\.0 85\.0 A Excellent 1st", c)
    assert re.search(r"Basic Science 30\.0 45\.0 75\.0 A Excellent 3rd", c)
    assert "3rd out of 6" in c
    e = pdf_text(uploads.pdf_for(acc.students["E"], acc.term))
    for grade, remark in (("F", "Fail"), ("D", "Fair"), ("E", "Poor")):
        assert re.search(rf"\d\.0 {grade} {remark} \d+(st|nd|rd|th)", e), grade


def test_pdf_fetches_no_external_url(acc, caplog):
    import weasyprint.urls

    seen = []
    real = weasyprint.urls.URLFetcher.fetch

    def spy(self, url, headers=None):
        seen.append(url)
        return real(self, url, headers)

    with (
        mock.patch.object(weasyprint.urls.URLFetcher, "fetch", spy),
        caplog.at_level(logging.WARNING, logger="weasyprint"),
    ):
        pdf = render_card(acc.students["B"], acc.term)
    assert pdf.startswith(b"%PDF")
    assert seen and all(url.startswith("data:") for url in seen)  # the logo, inline
    assert "blocked URL" not in caplog.text


def test_logo_is_inlined_and_fetched_once_per_batch(acc, uploads, logo_open):
    ctx = card_context(acc.students["B"], acc.term)
    assert ctx["logo"].startswith("data:image/png;base64,")
    logo_open.reset_mock()
    _publish(acc)
    generate_report_cards(acc.arm, acc.term)
    assert logo_open.call_count == 1


def test_logo_falls_back_to_resized_public_url(acc):
    with (
        mock.patch(
            "apps.core.storage.TenantCloudinaryStorage.open", side_effect=NotImplementedError
        ),
        mock.patch("apps.results.cards.fetch_url", return_value=PNG) as fetch,
    ):
        ctx = card_context(acc.students["B"], acc.term)
    url = fetch.call_args.args[0]
    assert url.startswith("https://res.cloudinary.com/") and "w_240" in url
    assert fetch.call_args.kwargs["allowed_urls"] == frozenset({url})
    assert ctx["logo"].startswith("data:image/png;base64,")


def test_card_renders_without_logo_when_it_cannot_be_read(acc, caplog):
    with (
        mock.patch(
            "apps.core.storage.TenantCloudinaryStorage._open",
            side_effect=OSError("secret https://x/y"),
        ),
        caplog.at_level(logging.WARNING, logger="apps.results.cards"),
    ):
        ctx = card_context(acc.students["B"], acc.term)
        pdf = render_card(acc.students["B"], acc.term)
    assert ctx["logo"] is None
    assert pdf.startswith(b"%PDF")
    assert "OSError" in caplog.text and "secret" not in caplog.text


def test_third_term_card_has_term_columns_and_cumulative(school_a, logo_open, uploads):
    with schema_context("school_a"):
        w = _accuracy_class(term_number=3)
        b = w.students["B"]
        ctx = card_context(b, w.term)
        assert ctx["third_term"] is True
        text = pdf_text(render_card(b, w.term))
    assert "1st term" in text and "2nd term" in text and "3rd term" in text
    assert "Cumulative" in text
    # Maths 60 / 70 / 80 -> 70.00; English and Basic Science 60 / 70 / 70 -> 66.67.
    assert re.search(r"Mathematics .*60\.0 70\.0 80\.0 70\.00", text)
    assert "66.67" in text
    assert "67.78" in text  # cumulative average
    assert "Next term begins" not in text  # no later term exists


def test_first_term_card_has_no_cumulative(acc):
    ctx = card_context(acc.students["B"], acc.term)
    assert ctx["third_term"] is False
    assert ctx["next_term_start"] == date(2027, 1, 5)


# --- generation ----------------------------------------------------------------------------


def test_generate_stores_upserts_and_audits(acc, uploads):
    _publish(acc)
    assert generate_report_cards(acc.arm, acc.term) == 6
    b = acc.students["B"]
    card = ReportCard.objects.get(student=b, term=acc.term)
    assert card.file_name == f"report_cards/{acc.session.pk}/{acc.term.pk}/{b.pk}.pdf"
    assert card.version == 1 and card.arm_id == acc.arm.pk
    assert len(uploads.files) == 6
    assert all(f.startswith(b"%PDF") for f in uploads.files.values())

    generate_report_cards(acc.arm, acc.term)  # regenerate: overwrite, same names
    assert ReportCard.objects.filter(term=acc.term).count() == 6
    assert ReportCard.objects.get(pk=card.pk).version == 2
    assert len(uploads.files) == 6
    events = AuditEvent.objects.filter(action="results.report_cards_generated").order_by("pk")
    assert [e.after for e in events] == [
        {"arm_id": acc.arm.pk, "term_id": acc.term.pk, "count": 6}
    ] * 2


def test_upload_is_authenticated_and_overwrites(acc):
    _publish(acc)
    with mock.patch("cloudinary.uploader.upload", return_value={}) as up:
        generate_report_cards(acc.arm, acc.term)
    kwargs = up.call_args.kwargs
    assert kwargs["type"] == "authenticated" and kwargs["overwrite"] is True
    assert kwargs["resource_type"] == "raw"


def test_generate_does_nothing_unless_published(acc, uploads):
    ClassResult.objects.create(arm=acc.arm, term=acc.term, status=ClassResult.Status.APPROVED)
    assert generate_report_cards(acc.arm, acc.term) == 0
    assert not ReportCard.objects.exists() and not uploads.files
    assert not AuditEvent.objects.filter(action="results.report_cards_generated").exists()


def test_real_unlock_during_generation_stops_and_removes_this_runs_cards(
    acc, uploads, django_capture_on_commit_callbacks
):
    """unlock_class deletes the summaries and subject results with the status change; the run
    must stop cleanly and remove what it wrote itself (the unlock hook is disabled here)."""
    _publish(acc)
    admin = staff("school_admin")
    admin.is_verified = lambda: True
    sheets = list(ScoreSheet.objects.filter(term=acc.term))
    real = cards.render_card
    rendered = []

    def unlock_after_first_card(student, term, **kwargs):
        rendered.append(student.pk)
        if len(rendered) == 2:  # an admin unlocks once the 1st card is stored
            unlock_class(acc.arm, acc.term, sheets=sheets[:1], reason="Typo", by=admin)
        return real(student, term, **kwargs)

    with (
        mock.patch("apps.results.workflow.on_class_unlocked"),
        mock.patch("apps.results.cards.render_card", side_effect=unlock_after_first_card),
        django_capture_on_commit_callbacks(execute=True),  # files are deleted on commit
    ):
        assert generate_report_cards(acc.arm, acc.term) == 0
    assert len(rendered) == 2
    assert not ResultSummary.objects.filter(term=acc.term).exists()
    assert not ReportCard.objects.exists()
    assert len(uploads.files) == 1
    assert sorted(uploads.destroyed) == sorted(uploads.files)
    assert not AuditEvent.objects.filter(action="results.report_cards_generated").exists()


def test_status_only_unlock_during_generation_also_stops(
    acc, uploads, django_capture_on_commit_callbacks
):
    """The re-check under the lock catches an unlock that happened after the render."""
    _publish(acc)
    real = cards.render_card
    rendered = []

    def render_then_unlock(student, term, **kwargs):
        rendered.append(student.pk)
        pdf = real(student, term, **kwargs)
        if len(rendered) == 3:
            ClassResult.objects.filter(arm=acc.arm, term=acc.term).update(status="draft")
        return pdf

    with (
        mock.patch("apps.results.cards.render_card", side_effect=render_then_unlock),
        django_capture_on_commit_callbacks(execute=True),
    ):
        assert generate_report_cards(acc.arm, acc.term) == 0
    assert not ReportCard.objects.exists()
    assert len(uploads.files) == 2
    assert sorted(uploads.destroyed) == sorted(uploads.files)


def test_missing_results_while_still_published_skips_that_student(acc, uploads, caplog):
    _publish(acc)
    from apps.results.models import SubjectResult

    b = acc.students["B"]
    real = cards.render_card

    def render(student, term, **kwargs):
        if student.pk == b.pk:
            raise ResultSummary.DoesNotExist
        return real(student, term, **kwargs)

    with (
        mock.patch("apps.results.cards.render_card", side_effect=render),
        caplog.at_level(logging.WARNING, logger="apps.results.cards"),
    ):
        assert generate_report_cards(acc.arm, acc.term) == 5
    assert not ReportCard.objects.filter(student=b).exists()
    assert f"student {b.pk}" in caplog.text and "Pupil" not in caplog.text
    assert SubjectResult.objects.exists()
    klass = ClassResult.objects.get(arm=acc.arm, term=acc.term)
    assert klass.cards_error == ClassResult.CARDS_FAILED  # shown, with "Regenerate"


@pytest.mark.parametrize("where", ["render", "store"])
def test_one_card_failing_skips_it_and_records_a_generic_error(acc, uploads, caplog, where):
    _publish(acc)
    b = acc.students["B"]
    real_render, real_save = cards.render_card, cards._save_card

    def render(student, term, **kwargs):
        if where == "render" and student.pk == b.pk:
            raise OSError(f"Pango choked on {student.last_name}")
        return real_render(student, term, **kwargs)

    def save(student, term, arm, pdf):
        if where == "store" and student.pk == b.pk:
            raise ConnectionError(f"Cloudinary 503 for {student.last_name}")
        return real_save(student, term, arm, pdf)

    with (
        mock.patch("apps.results.cards.render_card", side_effect=render),
        mock.patch("apps.results.cards._save_card", side_effect=save),
        caplog.at_level(logging.WARNING, logger="apps.results.cards"),
    ):
        assert generate_report_cards(acc.arm, acc.term) == 5
    assert not ReportCard.objects.filter(student=b).exists()
    assert ReportCard.objects.count() == 5
    klass = ClassResult.objects.get(arm=acc.arm, term=acc.term)
    assert klass.cards_error == ClassResult.CARDS_FAILED
    assert b.last_name not in klass.cards_error
    assert f"student {b.pk}" in caplog.text
    assert ("OSError" if where == "render" else "ConnectionError") in caplog.text
    assert b.last_name not in caplog.text and "Pupil" not in caplog.text
    assert any(rec.exc_info for rec in caplog.records)

    generate_report_cards(acc.arm, acc.term)  # regenerating clears it
    assert ClassResult.objects.get(pk=klass.pk).cards_error == ""


def test_whole_run_failing_records_a_generic_error(acc, uploads, caplog):
    _publish(acc)
    with (
        mock.patch("apps.results.cards.load_logo", side_effect=RuntimeError("Pupil secret")),
        caplog.at_level(logging.WARNING, logger="apps.results.cards"),
    ):
        assert generate_report_cards(acc.arm, acc.term) == 0
    klass = ClassResult.objects.get(arm=acc.arm, term=acc.term)
    assert klass.cards_error == ClassResult.CARDS_FAILED
    assert "RuntimeError" in caplog.text and "Pupil" not in caplog.text


def test_a_successful_run_keeps_a_notification_queueing_error(acc, uploads):
    _publish(acc)
    ClassResult.objects.filter(arm=acc.arm, term=acc.term).update(
        cards_error=ClassResult.NOTIFY_START_ERROR
    )
    assert generate_report_cards(acc.arm, acc.term) == 6
    klass = ClassResult.objects.get(arm=acc.arm, term=acc.term)
    assert klass.cards_error == ClassResult.NOTIFY_START_ERROR


def test_a_failed_run_keeps_a_notification_queueing_error(acc, uploads):
    """A card failure must not hide that guardian notifications never started."""
    _publish(acc)
    ClassResult.objects.filter(arm=acc.arm, term=acc.term).update(
        cards_error=ClassResult.NOTIFY_START_ERROR
    )
    with mock.patch("apps.results.cards.load_logo", side_effect=RuntimeError("boom")):
        assert generate_report_cards(acc.arm, acc.term) == 0
    klass = ClassResult.objects.get(arm=acc.arm, term=acc.term)
    assert klass.cards_error == ClassResult.NOTIFY_START_ERROR


def test_storage_limit_stops_and_shows_on_class_page(acc, uploads, client_a, caplog):
    _publish(acc)
    with (
        mock.patch(
            "apps.tenancy.limits.PlanLimits.check_can_store",
            side_effect=[None, PlanLimitExceeded("Storage limit for your plan has been reached.")],
        ),
        caplog.at_level(logging.WARNING, logger="apps.results.cards"),
    ):
        assert generate_report_cards(acc.arm, acc.term) == 1
    assert ReportCard.objects.count() == 1
    klass = ClassResult.objects.get(arm=acc.arm, term=acc.term)
    assert klass.cards_error == "Storage limit reached"
    assert "storage limit" in caplog.text.lower()
    assert "Pupil" not in caplog.text

    admin = staff("principal")
    from tests.accounts.helpers import login_verified

    login_verified(client_a, admin)
    r = client_a.get(reverse("results:class_status", args=[acc.arm.pk]))
    assert "Report cards could not be generated: Storage limit reached" in r.content.decode()

    generate_report_cards(acc.arm, acc.term)  # a later full run clears the error
    assert ClassResult.objects.get(pk=klass.pk).cards_error == ""


# --- hooks, end to end ---------------------------------------------------------------------


def _approved(w):
    principal = staff("principal")
    ClassResult.objects.update_or_create(
        arm=w.arm,
        term=w.term,
        defaults={
            "status": ClassResult.Status.APPROVED,
            "approved_at": timezone.now() - timedelta(minutes=5),
        },
    )
    ResultSummary.objects.filter(term=w.term).update(computed_at=timezone.now())
    return principal


def test_publish_enqueues_generation_on_reports_queue(acc, django_capture_on_commit_callbacks):
    principal = _approved(acc)
    with (
        mock.patch("apps.results.tasks.generate_report_cards_task.apply_async") as enqueue,
        django_capture_on_commit_callbacks(execute=True),
    ):
        publish_class(acc.arm, acc.term, by=principal)
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["args"] == [acc.arm.pk, acc.term.pk]
    assert enqueue.call_args.kwargs["queue"] == "reports"


def test_publish_then_unlock_end_to_end(acc, uploads, settings, django_capture_on_commit_callbacks):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    principal = _approved(acc)
    with django_capture_on_commit_callbacks(execute=True):
        publish_class(acc.arm, acc.term, by=principal)
    assert ReportCard.objects.filter(term=acc.term, arm=acc.arm).count() == 6
    assert len(uploads.files) == 6

    admin = staff("school_admin")
    admin.is_verified = lambda: True
    sheets = list(ScoreSheet.objects.filter(term=acc.term))
    with django_capture_on_commit_callbacks(execute=True):
        unlock_class(acc.arm, acc.term, sheets=sheets[:1], reason="Typo", by=admin)
    assert not ReportCard.objects.exists()
    assert sorted(uploads.destroyed) == sorted(uploads.files)


def test_unlock_hook_logs_file_delete_failure_by_type_only(
    acc, uploads, django_capture_on_commit_callbacks, caplog
):
    _publish(acc)
    generate_report_cards(acc.arm, acc.term)
    ClassResult.objects.filter(arm=acc.arm, term=acc.term).update(status="draft")
    with (
        mock.patch("cloudinary.uploader.destroy", side_effect=RuntimeError("Pupil B secret")),
        caplog.at_level(logging.WARNING, logger="apps.results.cards"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        cards.remove_report_cards(acc.arm, acc.term)
    assert not ReportCard.objects.exists()
    assert "RuntimeError" in caplog.text and "Pupil" not in caplog.text


def test_task_runs_generation(acc, uploads):
    from apps.results.tasks import generate_report_cards_task

    _publish(acc)
    generate_report_cards_task.apply(
        args=[acc.arm.pk, acc.term.pk], headers={"schema_name": "school_a"}
    )
    assert ReportCard.objects.count() == 6


# --- downloads -----------------------------------------------------------------------------


@pytest.fixture
def dl(school_a):
    with schema_context("school_a"):
        w = make_sheet_world()
        st, other = w.students[0], w.students[1]
        card = make_card(st, w.term, w.arm)
        other_card = make_card(other, w.term, w.arm)
        level = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
        arm2 = ClassArm.objects.create(level=level, suffix="B")
        teacher2 = staff("teacher")
        subject = Subject.objects.create(code="ENG", name="English", sections=["jss"])
        SubjectOffering.objects.create(
            subject=subject, arm=arm2, session=w.session, teacher=teacher2
        )
        ft = staff("teacher")
        FormTeacher.objects.create(arm=w.arm, session=w.session, teacher=ft)
        yield SimpleNamespace(
            w=w,
            st=st,
            other=other,
            card=card,
            other_card=other_card,
            other_teacher=teacher2,
            ft=ft,
            admin=staff("school_admin"),
        )


def card_url(student_pk, term_pk) -> str:
    return reverse("results:report_card", args=[student_pk, term_pk])


def _assert_signed_redirect(r, card):
    assert r.status_code == 302
    assert r["Cache-Control"] == "no-store"
    location = r["Location"]
    assert location.startswith("https://api.cloudinary.com/")
    assert "signature=" in location and "expires_at=" in location
    assert f"report_cards/{card.term.session_id}/{card.term_id}/{card.student_id}" in (
        location.replace("%2F", "/")
    )


def test_student_downloads_own_card_after_publish(client_a, dl):
    client_a.force_login(dl.st.user)
    r = client_a.get(card_url(dl.st.pk, dl.w.term.pk))
    _assert_signed_redirect(r, dl.card)


def test_student_card_404_before_publish(client_a, dl):
    with schema_context("school_a"):
        ClassResult.objects.filter(arm=dl.w.arm, term=dl.w.term).update(status="approved")
    client_a.force_login(dl.st.user)
    assert client_a.get(card_url(dl.st.pk, dl.w.term.pk)).status_code == 404


def test_student_card_from_an_earlier_class_is_404(client_a, dl):
    """The student moved arm: the stored card was generated for the old class."""
    with schema_context("school_a"):
        level = ClassLevel.objects.create(name="Basic 6", section="jss", order=8)
        old_arm = ClassArm.objects.create(level=level, suffix="Z")
        ReportCard.objects.filter(pk=dl.card.pk).update(arm=old_arm)
    client_a.force_login(dl.st.user)
    assert client_a.get(card_url(dl.st.pk, dl.w.term.pk)).status_code == 404


def test_student_cannot_download_another_students_card(client_a, dl):
    client_a.force_login(dl.st.user)
    assert client_a.get(card_url(dl.other.pk, dl.w.term.pk)).status_code == 404


def test_teacher_of_another_arm_gets_404(client_a, dl):
    client_a.force_login(dl.other_teacher)
    assert client_a.get(card_url(dl.st.pk, dl.w.term.pk)).status_code == 404


def test_staff_who_can_see_the_student_download(client_a, dl):
    client_a.force_login(dl.ft)
    _assert_signed_redirect(client_a.get(card_url(dl.st.pk, dl.w.term.pk)), dl.card)
    client_a.force_login(dl.w.teacher)
    _assert_signed_redirect(client_a.get(card_url(dl.st.pk, dl.w.term.pk)), dl.card)
    from tests.accounts.helpers import login_verified

    login_verified(client_a, dl.admin)
    _assert_signed_redirect(client_a.get(card_url(dl.other.pk, dl.w.term.pk)), dl.other_card)


def test_missing_card_is_404(client_a, dl):
    with schema_context("school_a"):
        ReportCard.objects.filter(pk=dl.card.pk).delete()
    client_a.force_login(dl.ft)
    assert client_a.get(card_url(dl.st.pk, dl.w.term.pk)).status_code == 404
    client_a.force_login(dl.st.user)
    assert client_a.get(card_url(dl.st.pk, dl.w.term.pk)).status_code == 404


@pytest.mark.parametrize("student,term", [("x", "1"), ("1", "١"), ("9" * 19, "1"), ("0", "0")])
def test_bad_ids_are_404(client_a, dl, student, term):
    client_a.force_login(dl.ft)
    r = client_a.get(f"/results/report-card/{student}/{term}/")
    assert r.status_code == 404


def test_anonymous_is_sent_to_login(client_a, dl):
    r = client_a.get(card_url(dl.st.pk, dl.w.term.pk))
    assert r.status_code == 302 and "login" in r["Location"]


# --- performance ---------------------------------------------------------------------------


@pytest.mark.slow
def test_fifty_students_under_two_minutes(school_a, logo_open, uploads):
    with schema_context("school_a"):
        from apps.academics.models import Term

        Term.objects.filter(is_current=True).update(is_current=False)
        session = default_session()
        term = _term(session, 1, current=True)
        level = ClassLevel.objects.create(name="JSS 3", section="jss", order=11)
        arm = ClassArm.objects.create(level=level, suffix="P")
        comps = list(_jss_structure())
        teacher = StaffFactory()
        names = [f"Subject {i}" for i in range(1, 11)]
        offerings = _offerings(arm, session, teacher, names)
        students = [
            StudentProfileFactory(enrolment__arm=arm, enrolment__session=session) for _ in range(50)
        ]
        Score.objects.bulk_create(
            Score(
                student=st,
                offering=off,
                term=term,
                component=comp,
                value=Decimal((i * 7 + j * 3) % (comp.max_score + 1)),
                entered_by=teacher,
                version=1,
            )
            for i, st in enumerate(students)
            for j, off in enumerate(offerings.values())
            for comp in comps
        )
        _submit(offerings.values(), term)
        compute_class(arm, term)
        for st in students:
            ReportComment.objects.create(
                student=st, term=term, class_teacher_comment="Good", principal_comment="Good"
            )
        _branding()
        ClassResult.objects.create(arm=arm, term=term, status=ClassResult.Status.PUBLISHED)
        assert AssessmentComponent.objects.filter(section="jss").count() == 2

        started = time.monotonic()
        count = generate_report_cards(arm, term)
        elapsed = time.monotonic() - started
    print(f"\n50 report cards rendered and stored in {elapsed:.1f} s")
    assert count == 50 and len(uploads.files) == 50
    assert elapsed < 120
