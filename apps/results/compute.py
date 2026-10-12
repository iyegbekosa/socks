"""Result computation (FR-07).

The pure core (``subject_total`` .. ``compute_class_data``) takes plain data and never touches
the database. ``compute_class`` gathers one class's data, runs the core and rewrites the class's
``SubjectResult`` and ``ResultSummary`` rows in one transaction.

All arithmetic is ``Decimal`` with ROUND_HALF_UP; floats are never used.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from apps.academics.models import ClassArm, SubjectOffering, Term
from apps.core.locks import school_lock
from apps.results.models import (
    AssessmentComponent,
    ResultSummary,
    Score,
    ScoreSheet,
    SubjectResult,
    components_complete,
    components_for,
    grade_for,
)
from apps.results.scope import ranked_students, required_offerings, students_taking

logger = logging.getLogger(__name__)

ONE_DP = Decimal("0.1")
TWO_DP = Decimal("0.01")
WHOLE = Decimal("1")
GradeFn = Callable[[Decimal], tuple[str, str]]


# --- pure helpers --------------------------------------------------------------------------


def subject_total(values: Iterable[Decimal]) -> Decimal:
    """Sum of component scores, kept to 1 decimal place."""
    return sum(values, Decimal(0)).quantize(ONE_DP, rounding=ROUND_HALF_UP)


def round_avg(total: Decimal, n: int) -> Decimal:
    """``total / n`` rounded half-up to 2 decimal places."""
    if n <= 0:
        raise ValueError("Cannot average over zero items.")
    return (Decimal(total) / Decimal(n)).quantize(TWO_DP, rounding=ROUND_HALF_UP)


def competition_rank[K: Hashable](values: Mapping[K, Decimal]) -> dict[K, int]:
    """Higher is better. Ties share a position and the next position is skipped (1, 2, 2, 4)."""
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    ranks: dict[K, int] = {}
    previous: Decimal | None = None
    position = 0
    for index, (key, value) in enumerate(ordered, start=1):
        if previous is None or value != previous:
            position = index
            previous = value
        ranks[key] = position
    return ranks


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st, 101st, 111th, 112th ..."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def stats(totals: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """(highest, lowest, average to 2 dp half-up)."""
    if not totals:
        raise ValueError("No totals.")
    return max(totals), min(totals), round_avg(sum(totals, Decimal(0)), len(totals))


def grade_lookup(bands: Iterable[Any]) -> GradeFn:
    """A pure grade function from ``(lower, upper, grade, remark)`` bands: the total is rounded
    half-up to a whole number, then matched to its band."""
    rows = [tuple(b) for b in bands]

    def grade(total: Decimal) -> tuple[str, str]:
        rounded = int(Decimal(total).quantize(WHOLE, rounding=ROUND_HALF_UP))
        for lower, upper, label, remark in rows:
            if lower <= rounded <= upper:
                return str(label), str(remark)
        raise LookupError(f"No grade band for {rounded}.")

    return grade


@dataclass(frozen=True)
class ComponentSpec:
    id: int
    name: str
    max_score: int


@dataclass(frozen=True)
class OfferingSpec:
    id: int
    subject_id: int
    subject_name: str
    teacher_name: str


@dataclass(frozen=True)
class SubjectRow:
    student_id: int
    offering_id: int
    subject_name: str
    teacher_name: str
    components: list[dict[str, Any]]
    total: Decimal
    grade: str
    remark: str
    position: int
    highest: Decimal
    lowest: Decimal
    class_average: Decimal
    cumulative: Decimal | None


@dataclass(frozen=True)
class SummaryRow:
    student_id: int
    subjects_count: int
    total: Decimal
    average: Decimal
    position: int
    out_of: int
    cumulative_average: Decimal | None


@dataclass
class ClassData:
    subjects: list[SubjectRow] = field(default_factory=list)
    summaries: list[SummaryRow] = field(default_factory=list)
    # Ranked candidates who take no subject (left out of every ranking).
    excluded: list[int] = field(default_factory=list)


def compute_class_data(
    *,
    student_ids: Sequence[int],
    offerings: Sequence[OfferingSpec],
    components: Sequence[ComponentSpec],
    scores: Mapping[int, Mapping[int, Mapping[int, Decimal]]],
    grade: GradeFn,
    term_number: int,
    prior_totals: Mapping[int, Mapping[int, Sequence[Decimal]]] | None = None,
) -> ClassData:
    """Compute one class's results from plain data.

    - ``student_ids``: the students eligible for ranking (active, actively enrolled).
    - ``scores[student][offering][component]``: a student takes an offering exactly when the
      offering is a key of ``scores[student]``; every component must then have a value.
    - ``prior_totals[student][subject_id]``: the student's totals for that subject in the
      session's earlier terms (3rd term only; only the terms present).
    """
    data = ClassData()
    eligible = [s for s in student_ids if scores.get(s)]
    data.excluded = [s for s in student_ids if not scores.get(s)]

    totals: dict[tuple[int, int], Decimal] = {}
    for st in eligible:
        for off in offerings:
            values = scores[st].get(off.id)
            if values is None:
                continue
            missing = [c.id for c in components if values.get(c.id) is None]
            if missing:
                raise ValueError(f"Offering {off.id}: {len(missing)} blank cell(s).")
            totals[(st, off.id)] = subject_total(values[c.id] for c in components)

    cumulative: dict[tuple[int, int], Decimal] = {}
    if term_number == Term.THIRD:
        prior = prior_totals or {}
        for (st, off_id), total in totals.items():
            subject_id = next(o.subject_id for o in offerings if o.id == off_id)
            series = [*prior.get(st, {}).get(subject_id, []), total]
            cumulative[(st, off_id)] = round_avg(sum(series, Decimal(0)), len(series))

    for off in offerings:
        takers = {st: t for (st, off_id), t in totals.items() if off_id == off.id}
        if not takers:
            continue
        highest, lowest, average = stats(list(takers.values()))
        positions = competition_rank(takers)
        for st in eligible:
            if st not in takers:
                continue
            total = takers[st]
            label, remark = grade(total)
            values = scores[st][off.id]
            data.subjects.append(
                SubjectRow(
                    student_id=st,
                    offering_id=off.id,
                    subject_name=off.subject_name,
                    teacher_name=off.teacher_name,
                    components=[
                        {
                            "name": c.name,
                            "max": c.max_score,
                            "score": str(Decimal(values[c.id]).quantize(ONE_DP)),
                        }
                        for c in components
                    ],
                    total=total,
                    grade=label,
                    remark=remark,
                    position=positions[st],
                    highest=highest,
                    lowest=lowest,
                    class_average=average,
                    cumulative=cumulative.get((st, off.id)),
                )
            )

    averages: dict[int, Decimal] = {}
    per_student: dict[int, list[Decimal]] = defaultdict(list)
    per_student_cum: dict[int, list[Decimal]] = defaultdict(list)
    for (st, off_id), total in totals.items():
        per_student[st].append(total)
        if (st, off_id) in cumulative:
            per_student_cum[st].append(cumulative[(st, off_id)])
    for st in eligible:
        averages[st] = round_avg(sum(per_student[st], Decimal(0)), len(per_student[st]))
    class_positions = competition_rank(averages)
    for st in eligible:
        cums = per_student_cum.get(st)
        data.summaries.append(
            SummaryRow(
                student_id=st,
                subjects_count=len(per_student[st]),
                total=subject_total(per_student[st]),
                average=averages[st],
                position=class_positions[st],
                out_of=len(eligible),
                cumulative_average=round_avg(sum(cums, Decimal(0)), len(cums)) if cums else None,
            )
        )
    return data


# --- database layer ------------------------------------------------------------------------


def _ids(values: Iterable[int]) -> str:
    return ", ".join(str(v) for v in sorted(values))


def prior_term_totals(
    student_ids: Sequence[int], term: Term
) -> dict[tuple[int, int, int], Decimal]:
    """Each student's per-subject totals in the session's earlier terms, keyed by
    ``(student_id, term_id, subject_id)``.

    Per (student, term, subject): the computed ``SubjectResult.total`` when present. Otherwise
    the sum of that term's scores, but only when the student has a non-null score for every
    component of the offering's section; a partly filled term counts as absent.
    """
    earlier = list(Term.objects.filter(session_id=term.session_id, number__lt=term.number))
    if not earlier:
        return {}
    found: dict[tuple[int, int, int], Decimal] = {}
    for st, term_id, subject_id, total in SubjectResult.objects.filter(
        student_id__in=student_ids, term__in=earlier
    ).values_list("student_id", "term_id", "offering__subject_id", "total"):
        found[(st, term_id, subject_id)] = total
    per_section = dict(
        AssessmentComponent.objects.values("section")
        .annotate(n=Count("pk"))
        .order_by()
        .values_list("section", "n")
    )
    raw = (
        Score.objects.filter(
            student_id__in=student_ids,
            term__in=earlier,
            value__isnull=False,
            component__section=F("offering__arm__level__section"),
        )
        .values("student_id", "term_id", "offering_id", "offering__subject_id")
        .annotate(
            total=Sum("value"),
            filled=Count("component", distinct=True),
            section=F("offering__arm__level__section"),
        )
        .order_by("offering_id")
    )
    for row in raw:
        if row["filled"] != per_section.get(row["section"]):
            continue
        key = (row["student_id"], row["term_id"], row["offering__subject_id"])
        found.setdefault(key, subject_total([row["total"]]))
    return found


def _prior_totals(student_ids: Sequence[int], term: Term) -> dict[int, dict[int, list[Decimal]]]:
    """Each student's per-subject totals in the session's earlier terms, in term order
    (see ``prior_term_totals``)."""
    found = prior_term_totals(student_ids, term)
    if not found:
        return {}
    earlier = Term.objects.filter(session_id=term.session_id, number__lt=term.number).order_by(
        "number"
    )
    out: dict[int, dict[int, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for t in earlier:
        for (st, term_id, subject_id), total in sorted(found.items()):
            if term_id == t.pk:
                out[st][subject_id].append(total)
    return out


def _teacher_name(offering: SubjectOffering) -> str:
    teacher = offering.teacher
    return teacher.get_full_name() if teacher is not None else ""


def compute_class(arm: ClassArm, term: Term) -> None:
    """Compute and store one class's results for a term (FR-07).

    Raises ValidationError (offering ids and counts only) when a sheet is not submitted, the
    section's components do not total 100, or a taker has blank cells.
    """
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        section = arm.level.section
        offerings = list(
            required_offerings(arm, term)
            .select_related("subject", "teacher")
            .order_by("subject__name", "pk")
        )
        if not offerings:
            raise ValidationError("This class has no subjects with students for the session.")
        submitted = set(
            ScoreSheet.objects.filter(
                offering__in=offerings, term=term, status=ScoreSheet.Status.SUBMITTED
            ).values_list("offering_id", flat=True)
        )
        pending = [o.pk for o in offerings if o.pk not in submitted]
        if pending:
            raise ValidationError(
                f"{len(pending)} of {len(offerings)} score sheet(s) are not submitted "
                f"(offerings {_ids(pending)})."
            )
        if not components_complete(section):
            raise ValidationError("The assessment components for this section do not total 100.")
        comps = components_for(section)

        ranked = list(ranked_students(arm, term).order_by("pk").values_list("pk", flat=True))
        ranked_set = set(ranked)
        scores: dict[int, dict[int, dict[int, Decimal]]] = defaultdict(dict)
        for off in offerings:
            for st in students_taking(off).order_by().values_list("pk", flat=True):
                if st in ranked_set:
                    scores[st][off.pk] = {}
        for st, off_id, comp_id, value in Score.objects.filter(
            offering__in=offerings, term=term, component__in=comps, value__isnull=False
        ).values_list("student_id", "offering_id", "component_id", "value"):
            cells = scores.get(st, {}).get(off_id)
            if cells is not None and value is not None:
                cells[comp_id] = value
        blanks: dict[int, int] = defaultdict(int)
        for per_offering in scores.values():
            for off_id, cells in per_offering.items():
                blanks[off_id] += len(comps) - len(cells)
        blanks = {k: v for k, v in blanks.items() if v}
        if blanks:
            detail = ", ".join(f"offering {k}: {v}" for k, v in sorted(blanks.items()))
            raise ValidationError(f"Some score cells are blank ({detail}).")

        cache: dict[int, tuple[str, str]] = {}

        def grade(total: Decimal) -> tuple[str, str]:
            key = int(Decimal(total).quantize(WHOLE, rounding=ROUND_HALF_UP))
            if key not in cache:
                try:
                    band = grade_for(section, total)
                except LookupError as exc:
                    raise ValidationError(str(exc)) from exc
                cache[key] = (band.grade, band.remark)
            return cache[key]

        data = compute_class_data(
            student_ids=ranked,
            offerings=[
                OfferingSpec(o.pk, o.subject_id, o.subject.name, _teacher_name(o))
                for o in offerings
            ],
            components=[ComponentSpec(c.pk, c.name, c.max_score) for c in comps],
            scores=scores,
            grade=grade,
            term_number=term.number,
            prior_totals=_prior_totals(ranked, term) if term.number == Term.THIRD else None,
        )
        for st in data.excluded:
            logger.warning("Student %s takes no subjects; left out of class ranking.", st)

        written = [s.student_id for s in data.summaries]
        # Every offering of the arm: one that has lost its takers keeps no stale rows.
        SubjectResult.objects.filter(
            term=term, offering__arm=arm, offering__session_id=term.session_id
        ).delete()
        ResultSummary.objects.filter(term=term, arm=arm).delete()
        ResultSummary.objects.filter(term=term, student_id__in=written).delete()
        SubjectResult.objects.bulk_create(
            SubjectResult(
                student_id=r.student_id,
                offering_id=r.offering_id,
                term=term,
                subject_name=r.subject_name,
                teacher_name=r.teacher_name,
                components=r.components,
                total=r.total,
                grade=r.grade,
                remark=r.remark,
                position=r.position,
                highest=r.highest,
                lowest=r.lowest,
                class_average=r.class_average,
                cumulative=r.cumulative,
            )
            for r in data.subjects
        )
        now = timezone.now()
        ResultSummary.objects.bulk_create(
            ResultSummary(
                student_id=s.student_id,
                term=term,
                arm=arm,
                subjects_count=s.subjects_count,
                total=s.total,
                average=s.average,
                position=s.position,
                out_of=s.out_of,
                cumulative_average=s.cumulative_average,
                computed_at=now,
            )
            for s in data.summaries
        )
