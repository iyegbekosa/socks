"""The student portal's own results page: published terms only, never anyone else's."""

from collections import defaultdict
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.results.compute import ordinal
from apps.results.models import (
    ClassResult,
    ReportCard,
    ResultSummary,
    SubjectResult,
    in_summary_arm,
)
from apps.students.models import Student


def published_summaries(student: Student) -> list[ResultSummary]:
    """The student's summaries whose class (the summary's own arm) is published for that term,
    newest term first."""
    published = ClassResult.objects.filter(
        arm_id=OuterRef("arm_id"),
        term_id=OuterRef("term_id"),
        status=ClassResult.Status.PUBLISHED,
    )
    return list(
        ResultSummary.objects.filter(student=student)
        .filter(Exists(published))
        .select_related("term__session", "arm__level")
        .order_by("-term__session__start_date", "-term__number", "-pk")
    )


def _withheld_term_ids(student: Student, summaries: list[ResultSummary]) -> set[int]:
    """Terms whose results are withheld for unpaid fees (UC-17). Imported here so results
    never imports fees at module level."""
    from apps.fees.blocking import blocking_enabled, owed_up_to

    # The setting is read once here; ``owed_up_to`` is ``results_withheld`` without re-reading it.
    if not summaries or not blocking_enabled():
        return set()
    return {s.term_id for s in summaries if owed_up_to(student, s.term) > 0}


@never_cache
@require_GET
@login_required
def my_results(request):
    if getattr(request.user, "kind", None) != "student":
        raise PermissionDenied
    student = Student.objects.filter(user=request.user).first()
    summaries = published_summaries(student) if student is not None else []
    terms: list[dict[str, Any]] = []
    if student is not None and summaries:
        from apps.fees.blocking import WITHHELD_MESSAGE

        withheld = _withheld_term_ids(student, summaries)
        shown = [s.term_id for s in summaries if s.term_id not in withheld]
        # One query for every subject row: each term's rows from the summary's own arm.
        by_term: dict[int, list[SubjectResult]] = defaultdict(list)
        for r in in_summary_arm(
            SubjectResult.objects.filter(student=student, term_id__in=shown)
        ).order_by("subject_name", "pk"):
            r.position_label = ordinal(r.position)  # type: ignore[attr-defined]
            by_term[r.term_id].append(r)
        cards = set(
            ReportCard.objects.filter(student=student, term_id__in=shown).values_list(
                "term_id", "arm_id"
            )
        )
        for s in summaries:
            if s.term_id in withheld:
                # Only the term heading and the message: none of the term's data.
                terms.append({"term": s.term, "withheld": True, "message": WITHHELD_MESSAGE})
                continue
            terms.append(
                {
                    "withheld": False,
                    "summary": s,
                    "term": s.term,
                    "position": ordinal(s.position),
                    "subjects": by_term.get(s.term_id, []),
                    "has_card": (s.term_id, s.arm_id) in cards,
                }
            )
    return render(request, "results/my_results.html", {"student": student, "terms": terms})
