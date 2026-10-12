"""Score sheets: reading the grid and saving versioned cells (design §5.4, FR-06/FR-08).

A cell is ``(student, offering, term, component)``. A cell that has never been saved has
version 0. Every save sends the version it read; any mismatch refuses the whole request.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.academics.models import SubjectOffering, Term
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.results.models import AssessmentComponent, ClassResult, Score, ScoreSheet
from apps.results.scope import students_taking

SCORE_RE = re.compile(r"^[0-9]{1,3}(\.[0-9])?$")
TERM_MESSAGE = "This term is not in the subject's session."
LOCKED_MESSAGE = "These results are locked, so scores can no longer be changed."


class ResultsLocked(Exception):
    """The sheet or its class result is past draft: scores are read-only."""

    def __init__(self, message: str = LOCKED_MESSAGE):
        super().__init__(message)
        self.message = message


class VersionConflict(Exception):
    """Some cells changed since the client read them. ``conflicts`` holds their current state:
    ``[{"student", "component", "value", "version"}]`` (value as a string, or None)."""

    def __init__(self, conflicts: list[dict]):
        super().__init__(f"{len(conflicts)} cell(s) changed since they were read.")
        self.conflicts = conflicts


@dataclass(frozen=True)
class CellInput:
    student_id: int
    component_id: int
    value: str | None
    version: int


@dataclass
class SaveResult:
    # One entry per submitted cell: {"student", "component", "version"} after the save.
    versions: list[dict] = field(default_factory=list)
    changed: int = 0


@dataclass
class SheetData:
    offering: SubjectOffering
    term: Term
    components: list[AssessmentComponent]
    # [{"student_id", "name", "admission_number", "cells": [{"component_id", "value",
    #   "version"}]}], cells in component order; missing cells are value None, version 0.
    rows: list[dict]
    sheet_status: str
    class_status: str
    editable: bool


def parse_score(raw: str | None) -> Decimal | None:
    """``None`` or ``""`` clears the cell. Otherwise, after stripping: 1-3 ASCII digits with at
    most one decimal place. Anything else raises ValueError."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError("not a string")
    text = raw.strip()
    if not SCORE_RE.match(text):
        raise ValueError("not a score")
    return Decimal(text).quantize(Decimal("0.1"))


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _check_term(offering: SubjectOffering, term: Term) -> None:
    if term.session_id != offering.session_id:
        raise ValidationError(TERM_MESSAGE)


def _status_rows(offering: SubjectOffering, term: Term) -> tuple[ScoreSheet, ClassResult]:
    sheet, _ = ScoreSheet.objects.get_or_create(offering=offering, term=term)
    klass, _ = ClassResult.objects.get_or_create(arm_id=offering.arm_id, term=term)
    return sheet, klass


def _editable(sheet: ScoreSheet, klass: ClassResult) -> bool:
    return sheet.status == ScoreSheet.Status.DRAFT and klass.status == ClassResult.Status.DRAFT


def _section(offering: SubjectOffering) -> str:
    return offering.arm.level.section


def sheet_rows(offering: SubjectOffering, term: Term) -> SheetData:
    _check_term(offering, term)
    sheet, klass = _status_rows(offering, term)
    components = list(AssessmentComponent.objects.filter(section=_section(offering)))
    students = list(students_taking(offering).select_related("user"))
    cells = {
        (s.student_id, s.component_id): s
        for s in Score.objects.filter(
            offering=offering, term=term, student_id__in=[st.pk for st in students]
        )
    }
    rows = []
    for st in students:
        row_cells = []
        for comp in components:
            score = cells.get((st.pk, comp.pk))
            row_cells.append(
                {
                    "component_id": comp.pk,
                    "value": _fmt(score.value) if score else None,
                    "version": score.version if score else 0,
                }
            )
        rows.append(
            {
                "student_id": st.pk,
                "name": st.full_name,
                "admission_number": st.admission_number,
                "cells": row_cells,
            }
        )
    return SheetData(
        offering=offering,
        term=term,
        components=components,
        rows=rows,
        sheet_status=sheet.status,
        class_status=klass.status,
        editable=_editable(sheet, klass),
    )


def _validate(
    offering: SubjectOffering, cells: list[CellInput]
) -> dict[tuple[int, int], Decimal | None]:
    """Parse every cell or raise one ValidationError keyed ``"<student_id>:<component_id>"``."""
    taking = set(students_taking(offering).values_list("pk", flat=True))
    # Row locks: save_components locks the same rows, so a max cannot change mid-save.
    maxima = dict(
        AssessmentComponent.objects.select_for_update()
        .filter(section=_section(offering))
        .values_list("pk", "max_score")
    )
    errors: dict[str, list[str]] = {}
    parsed: dict[tuple[int, int], Decimal | None] = {}
    for c in cells:
        key = (c.student_id, c.component_id)
        label = f"{c.student_id}:{c.component_id}"

        def fail(message: str, label: str = label) -> None:
            errors.setdefault(label, []).append(message)

        if key in parsed or label in errors:
            fail("This cell appears more than once in the save.")
            continue
        if c.student_id not in taking:
            fail("This student does not take this subject.")
        if c.component_id not in maxima:
            fail("This assessment component does not apply to this class.")
        try:
            value = parse_score(c.value)
        except ValueError:
            fail("Enter a number with at most one decimal place, or leave it blank.")
            continue
        if value is not None and c.component_id in maxima and value > maxima[c.component_id]:
            fail(f"Enter a score from 0 to {maxima[c.component_id]}.")
        if label not in errors:
            parsed[key] = value
    if errors:
        raise ValidationError(errors)
    return parsed


def save_cells(
    offering: SubjectOffering,
    term: Term,
    cells: list[CellInput],
    *,
    by: Any,
    request: Any = None,
) -> SaveResult:
    """Save score cells all-or-nothing.

    Raises ``ValidationError`` (per-cell, keyed ``"<student_id>:<component_id>"``),
    ``VersionConflict`` or ``ResultsLocked``; in each case nothing is written. A refusal
    because results are locked is audited as ``results.edit_refused``, and that audit row
    is written after the rolled-back block so it survives.
    """
    _check_term(offering, term)
    try:
        return _save_cells(offering, term, cells, by=by, request=request)
    except ResultsLocked:
        record(
            "results.edit_refused",
            request=request,
            actor=by,
            after={"offering_id": offering.pk, "term_id": term.pk},
        )
        raise


def _save_cells(
    offering: SubjectOffering, term: Term, cells: list[CellInput], *, by: Any, request: Any
) -> SaveResult:
    with transaction.atomic():
        school_lock(f"scores:{offering.pk}:{term.pk}")
        _status_rows(offering, term)
        sheet = ScoreSheet.objects.select_for_update().get(offering=offering, term=term)
        klass = ClassResult.objects.select_for_update().get(arm_id=offering.arm_id, term=term)
        if not _editable(sheet, klass):
            raise ResultsLocked()

        parsed = _validate(offering, cells)

        rows = {
            (s.student_id, s.component_id): s
            for s in Score.objects.select_for_update().filter(
                offering=offering,
                term=term,
                student_id__in={c.student_id for c in cells},
                component_id__in={c.component_id for c in cells},
            )
        }
        conflicts = []
        for c in cells:
            row = rows.get((c.student_id, c.component_id))
            current = row.version if row else 0
            if c.version != current:
                conflicts.append(
                    {
                        "student": c.student_id,
                        "component": c.component_id,
                        "value": _fmt(row.value) if row else None,
                        "version": current,
                    }
                )
        if conflicts:
            raise VersionConflict(conflicts)

        result = SaveResult()
        audit = sheet.first_submitted_at is not None
        for c in cells:
            key = (c.student_id, c.component_id)
            new = parsed[key]
            row = rows.get(key)
            old = row.value if row else None
            if new == old:  # unchanged (versions already match): skip, no audit
                result.versions.append(
                    {"student": c.student_id, "component": c.component_id, "version": c.version}
                )
                continue
            if row is None:
                row = Score.objects.create(
                    student_id=c.student_id,
                    offering=offering,
                    term=term,
                    component_id=c.component_id,
                    value=new,
                    version=1,
                    entered_by=by,
                )
            else:
                row.value = new
                row.version += 1
                row.entered_by = by
                row.save(update_fields=["value", "version", "entered_by", "updated_at"])
            result.changed += 1
            result.versions.append(
                {"student": c.student_id, "component": c.component_id, "version": row.version}
            )
            if audit:
                ids = {"student_id": c.student_id, "component_id": c.component_id}
                record(
                    "results.score_changed",
                    request=request,
                    actor=by,
                    obj=sheet,
                    before={**ids, "value": _fmt(old)},
                    after={**ids, "value": _fmt(new)},
                )
        return result
