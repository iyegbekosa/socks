"""Report-card PDFs (FR-09): build the context, render, store, remove and authorise downloads.

Cards are stored privately (``default_storage``, Cloudinary ``authenticated``) at
``report_cards/<session pk>/<term pk>/<student pk>.pdf`` and overwritten on regeneration.

Generation renders each PDF outside any lock or transaction, then saves it in a short
transaction holding ``results:{arm}:{term}`` after re-checking the class is still published.
Logs and audit rows carry ids, counts and exception types only.
"""

import base64
import io
import logging
from collections.abc import Iterable
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.db import connection, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from apps.academics.models import ClassArm, Term
from apps.academics.scope import students_visible_to
from apps.audit.services import record
from apps.branding.models import LOGO_WIDTH, get_branding
from apps.core.locks import school_lock
from apps.core.parsing import parse_id
from apps.core.pdf import fetch_url, render_pdf
from apps.results.compute import ordinal, prior_term_totals
from apps.results.models import (
    ClassResult,
    GradeBand,
    Rating,
    ReportCard,
    ReportComment,
    ResultSummary,
    SubjectResult,
    Trait,
)
from apps.students.models import Student
from apps.tenancy.limits import PlanLimitExceeded

logger = logging.getLogger(__name__)

STORAGE_ERROR = "Storage limit reached"
_LOAD = object()  # card_context(logo=...) default: read the school logo now


class _NotPublished(Exception):
    """The class was unlocked while its cards were being generated."""


def card_name(student: Student, term: Term) -> str:
    return f"report_cards/{term.session_id}/{term.pk}/{student.pk}.pdf"


def _lock(arm: ClassArm, term: Term) -> None:
    school_lock(f"results:{arm.pk}:{term.pk}")


# --- logo ----------------------------------------------------------------------------------


def _png_data_uri(raw: bytes) -> str:
    """Re-encode an image as a PNG at most ``LOGO_WIDTH`` px wide, as a ``data:`` URI."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as opened:
        opened.load()
        img: Image.Image = opened
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")
        img.thumbnail((LOGO_WIDTH, LOGO_WIDTH * 4))
        out = io.BytesIO()
        img.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def load_logo() -> str | None:
    """The school logo as a ``data:`` URI, or None when there is none or it cannot be read.

    Read through the public storage; a storage that cannot open files falls back to the
    logo's own resized public URL, fetched with the PDF renderer's allow-list fetcher.
    """
    logo = get_branding().logo
    name = logo.name if logo else ""
    if not name:
        return None
    storage = storages["public"]
    try:
        try:
            with storage.open(name) as fh:
                raw = fh.read()
        except NotImplementedError:
            url = storage.image_url(name, width=LOGO_WIDTH)  # type: ignore[attr-defined]
            raw = fetch_url(url, allowed_urls=frozenset({url}))
        return _png_data_uri(raw)
    except Exception as exc:  # noqa: BLE001 - a card without a logo beats no card
        logger.warning(
            "Report-card logo unavailable (%s); rendering without it.", type(exc).__name__
        )
        return None


# --- context and rendering -----------------------------------------------------------------


def _school_name() -> str:
    from apps.tenancy.models import School

    tenant = getattr(connection, "tenant", None)
    if isinstance(tenant, School):
        return tenant.name
    school = School.objects.filter(schema_name=getattr(connection, "schema_name", "")).first()
    return school.name if school is not None else ""


def _next_term(term: Term) -> Term | None:
    return (
        Term.objects.filter(
            Q(session__start_date__gt=term.session.start_date)
            | Q(session_id=term.session_id, number__gt=term.number)
        )
        .exclude(pk=term.pk)
        .order_by("session__start_date", "number", "pk")
        .first()
    )


def _component_headers(results: Iterable[SubjectResult]) -> list[dict[str, Any]]:
    """Component columns from the snapshots, in order of first appearance."""
    headers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in results:
        for comp in r.components or []:
            name = str(comp.get("name", ""))
            if name not in seen:
                seen.add(name)
                headers.append({"name": name, "max": comp.get("max")})
    return headers


def _ratings(student: Student, term: Term) -> dict[str, list[dict[str, Any]]]:
    values = dict(
        Rating.objects.filter(student=student, term=term).values_list("trait_id", "value")
    )
    out: dict[str, list[dict[str, Any]]] = {Trait.Kind.AFFECTIVE: [], Trait.Kind.PSYCHOMOTOR: []}
    for trait in Trait.objects.all():
        out.setdefault(trait.kind, []).append({"name": trait.name, "value": values.get(trait.pk)})
    return out


def card_context(student: Student, term: Term, *, logo: Any = _LOAD) -> dict[str, Any]:
    """Everything a report card shows (FR-09). ``logo`` is a ``data:`` URI or None; by default
    the school logo is read now (pass it in to read it once per batch)."""
    if logo is _LOAD:
        logo = load_logo()
    term = Term.objects.select_related("session").get(pk=term.pk)
    summary = ResultSummary.objects.select_related("arm__level").get(student=student, term=term)
    arm = summary.arm
    results = list(
        SubjectResult.objects.filter(student=student, term=term)
        .select_related("offering")
        .order_by("subject_name", "pk")
    )
    headers = _component_headers(results)
    third_term = term.number == Term.THIRD
    earlier: dict[tuple[int, int, int], Any] = {}
    earlier_terms: list[Term] = []
    if third_term:
        earlier = prior_term_totals([student.pk], term)
        earlier_terms = list(
            Term.objects.filter(session_id=term.session_id, number__lt=term.number).order_by(
                "number"
            )
        )
    rows = []
    for r in results:
        scores = {str(c.get("name", "")): c.get("score") for c in r.components or []}
        row: dict[str, Any] = {
            "subject": r.subject_name,
            "scores": [scores.get(h["name"]) for h in headers],
            "total": r.total,
            "grade": r.grade,
            "remark": r.remark,
            "position": ordinal(r.position),
            "highest": r.highest,
            "lowest": r.lowest,
            "class_average": r.class_average,
            "cumulative": r.cumulative,
        }
        if third_term:
            by_number = {
                t.number: earlier.get((student.pk, t.pk, r.offering.subject_id))
                for t in earlier_terms
            }
            row["term_totals"] = [by_number.get(1), by_number.get(2), r.total]
        rows.append(row)
    comment = ReportComment.objects.filter(student=student, term=term).first()
    branding = get_branding()
    nxt = _next_term(term)
    return {
        "school_name": _school_name(),
        "motto": branding.motto,
        "address": branding.address,
        "primary_color": branding.primary_color,
        "accent_color": branding.accent_color,
        "logo": logo,
        "student_name": student.full_name,
        "admission_number": student.admission_number or "",
        "class_name": arm.display_name,
        "session_name": term.session.name,
        "term_label": term.get_number_display(),
        "days_present": comment.days_present if comment else None,
        "days_open": comment.days_open if comment else None,
        "components": headers,
        "rows": rows,
        "third_term": third_term,
        "total": summary.total,
        "average": summary.average,
        "subjects_count": summary.subjects_count,
        "position": ordinal(summary.position),
        "out_of": summary.out_of,
        "cumulative_average": summary.cumulative_average,
        "ratings": _ratings(student, term),
        "class_teacher_comment": comment.class_teacher_comment if comment else "",
        "principal_comment": comment.principal_comment if comment else "",
        "next_term_start": nxt.start_date if nxt else None,
        "grading_key": list(GradeBand.objects.filter(section=arm.level.section)),
    }


def render_card(student: Student, term: Term, *, logo: Any = _LOAD) -> bytes:
    """The student's report card for ``term`` as PDF bytes. Nothing outside the page is
    fetched: the logo is inlined and ``allowed_urls`` is empty."""
    html = render_to_string("results/report_card.html", card_context(student, term, logo=logo))
    return render_pdf(html)


# --- generation and removal ----------------------------------------------------------------


def _is_published(arm: ClassArm, term: Term, *, lock: bool = False) -> bool:
    qs = ClassResult.objects.filter(arm=arm, term=term)
    if lock:
        qs = qs.select_for_update()
    klass = qs.first()
    return klass is not None and klass.status == ClassResult.Status.PUBLISHED


def _delete_files(names: list[str]) -> None:
    for name in names:
        try:
            default_storage.delete(name)
        except Exception as exc:  # noqa: BLE001 - one failed delete must not stop the rest
            logger.warning("Could not delete a report-card file (%s).", type(exc).__name__)


def _save_card(student: Student, term: Term, arm: ClassArm, pdf: bytes) -> str:
    """Store one card and upsert its row; the caller holds the class lock."""
    name = default_storage.save(card_name(student, term), ContentFile(pdf))
    now = timezone.now()
    card = ReportCard.objects.select_for_update().filter(student=student, term=term).first()
    if card is None:
        ReportCard.objects.create(
            student=student, term=term, arm=arm, file_name=name, generated_at=now
        )
    else:
        card.arm = arm
        card.file_name = name
        card.generated_at = now
        card.version += 1
        card.save(update_fields=["arm", "file_name", "generated_at", "version"])
    return name


def _discard(arm: ClassArm, term: Term, written: list[tuple[int, str]]) -> None:
    """Remove the cards this run wrote, unless the class has been published again since."""
    with transaction.atomic():
        _lock(arm, term)
        if _is_published(arm, term, lock=True):
            return
        ReportCard.objects.filter(term=term, student_id__in=[sid for sid, _ in written]).delete()
        names = [name for _, name in written]
        transaction.on_commit(lambda: _delete_files(names))


def _set_cards_error(arm: ClassArm, term: Term, error: str) -> None:
    with transaction.atomic():
        _lock(arm, term)
        ClassResult.objects.filter(arm=arm, term=term).exclude(cards_error=error).update(
            cards_error=error
        )


def _audit(arm: ClassArm, term: Term, count: int) -> None:
    klass = ClassResult.objects.filter(arm=arm, term=term).first()
    record(
        "results.report_cards_generated",
        obj=klass,
        after={"arm_id": arm.pk, "term_id": term.pk, "count": count},
    )


def generate_report_cards(arm: ClassArm, term: Term) -> int:
    """Render and store a card for every student with a result summary in this published
    class, returning how many were stored.

    Does nothing (0) unless the class is published. Stops and removes what it wrote (0) if
    the class is unlocked mid-run. Stops on the storage limit, recording ``cards_error``.
    """
    if not _is_published(arm, term):
        return 0
    student_ids = list(
        ResultSummary.objects.filter(arm=arm, term=term)
        .order_by("student_id")
        .values_list("student_id", flat=True)
    )
    logo = load_logo()
    written: list[tuple[int, str]] = []
    for student_id in student_ids:
        student = Student.objects.select_related("user").filter(pk=student_id).first()
        if student is None:
            continue
        pdf = render_card(student, term, logo=logo)  # slow: outside any lock
        try:
            with transaction.atomic():
                _lock(arm, term)
                if not _is_published(arm, term, lock=True):
                    raise _NotPublished
                name = _save_card(student, term, arm, pdf)
        except _NotPublished:
            logger.info(
                "Class %s term %s was unlocked during report-card generation; "
                "removing %s cards from this run.",
                arm.pk,
                term.pk,
                len(written),
            )
            _discard(arm, term, written)
            return 0
        except PlanLimitExceeded:
            logger.warning(
                "Report cards for class %s term %s stopped at the storage limit after %s of %s.",
                arm.pk,
                term.pk,
                len(written),
                len(student_ids),
            )
            _set_cards_error(arm, term, STORAGE_ERROR)
            _audit(arm, term, len(written))
            return len(written)
        written.append((student.pk, name))
    _set_cards_error(arm, term, "")
    _audit(arm, term, len(written))
    return len(written)


def remove_report_cards(arm: ClassArm, term: Term) -> int:
    """Delete this class's cards for the term (rows now, files on commit), unless the class
    is published again. Returns how many rows were removed."""
    with transaction.atomic():
        _lock(arm, term)
        klass = ClassResult.objects.select_for_update().filter(arm=arm, term=term).first()
        if klass is not None and klass.status == ClassResult.Status.PUBLISHED:
            return 0
        found = list(ReportCard.objects.filter(arm=arm, term=term).values_list("pk", "file_name"))
        ReportCard.objects.filter(pk__in=[pk for pk, _ in found]).delete()
        if klass is not None and klass.cards_error:
            klass.cards_error = ""
            klass.save(update_fields=["cards_error"])
        names = [name for _, name in found]
        transaction.on_commit(lambda: _delete_files(names))
    return len(found)


# --- downloads -----------------------------------------------------------------------------


def downloadable_card(user: Any, raw_student: Any, raw_term: Any) -> ReportCard | None:
    """The card this user may download, or None (a 404 for the caller).

    Students: their own card, only while their class for that term is published. Staff:
    students in ``students_visible_to``, and only when the card exists.
    """
    student_id, term_id = parse_id(str(raw_student)), parse_id(str(raw_term))
    if not student_id or not term_id:
        return None
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    kind = getattr(user, "kind", None)
    if kind == "student":
        if not Student.objects.filter(pk=student_id, user=user).exists():
            return None
        summary = ResultSummary.objects.filter(student_id=student_id, term_id=term_id).first()
        if (
            summary is None
            or not ClassResult.objects.filter(
                arm_id=summary.arm_id, term_id=term_id, status=ClassResult.Status.PUBLISHED
            ).exists()
        ):
            return None
    elif kind == "staff":
        if not students_visible_to(user).filter(pk=student_id).exists():
            return None
    else:
        return None
    return ReportCard.objects.filter(student_id=student_id, term_id=term_id).first()
