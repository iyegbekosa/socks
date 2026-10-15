"""Message audiences: who a message is for, resolved to active students. Labels never name
anyone."""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

from apps.academics.models import ClassArm, ClassLevel, Term
from apps.academics.services import current_session, current_term
from apps.core.money import naira_display
from apps.fees.debtors import debtors
from apps.students.models import Enrolment, Student

TYPES = ("school", "level", "arm", "debtors", "students")
MAX_STUDENTS = 50


@dataclass(frozen=True)
class Audience:
    type: str
    id: int | None = None
    ids: list[int] | tuple[int, ...] = field(default_factory=tuple)
    term: int | None = None  # Term pk, for debtors; the current term when None
    min_balance_kobo: int = 1


def _level(a: Audience) -> ClassLevel:
    level = ClassLevel.objects.filter(pk=a.id).first() if a.id else None
    if level is None:
        raise ValidationError("Choose a class level.")
    return level


def _arm(a: Audience) -> ClassArm:
    arm = ClassArm.objects.select_related("level").filter(pk=a.id).first() if a.id else None
    if arm is None:
        raise ValidationError("Choose a class.")
    return arm


def _term(a: Audience) -> Term:
    term = (
        Term.objects.select_related("session").filter(pk=a.term).first()
        if a.term
        else current_term()
    )
    if term is None:
        raise ValidationError("Choose a term.")
    return term


def _ids(a: Audience) -> list[int]:
    ids = list(dict.fromkeys(int(i) for i in a.ids))
    if not ids:
        raise ValidationError("Choose at least one student.")
    if len(ids) > MAX_STUDENTS:
        raise ValidationError(f"Choose at most {MAX_STUDENTS} students.")
    return ids


def _check_type(a: Audience) -> None:
    if a.type not in TYPES:
        raise ValidationError("Choose who the message is for.")


def resolve(a: Audience) -> list[Student]:
    """The active students in the audience, each once, in a stable order."""
    _check_type(a)
    active = Student.objects.filter(status=Student.Status.ACTIVE).select_related("user")
    if a.type == "students":
        return list(active.filter(pk__in=_ids(a)).order_by("pk"))
    if a.type == "debtors":
        rows = debtors(_term(a), min_balance_kobo=a.min_balance_kobo)
        return [r.student for r in rows if r.student.status == Student.Status.ACTIVE]
    session = current_session()
    if session is None:
        return []
    enrolled = Enrolment.objects.filter(session=session, status=Enrolment.Status.ACTIVE)
    if a.type == "level":
        enrolled = enrolled.filter(arm__level=_level(a))
    elif a.type == "arm":
        enrolled = enrolled.filter(arm=_arm(a))
    return list(active.filter(pk__in=enrolled.values("student_id")).order_by("pk"))


def label(a: Audience) -> str:
    """A short description without names, e.g. "JSS 1A" or "3 students"."""
    _check_type(a)
    if a.type == "school":
        return "School"
    if a.type == "level":
        return _level(a).name
    if a.type == "arm":
        return _arm(a).display_name
    if a.type == "debtors":
        term = _term(a)
        minimum = naira_display(max(1, a.min_balance_kobo))
        return f"Debtors ≥ {minimum} — {term.get_number_display()} {term.session.name}"
    n = len(_ids(a))
    return f"{n} student" if n == 1 else f"{n} students"
