"""School-day rules: Monday to Friday inside the term, excluding holidays."""

from datetime import date, timedelta

from django.utils import timezone

from apps.academics.models import Term
from apps.academics.services import current_term
from apps.attendance.models import Holiday


class RegisterDateError(Exception):
    NOT_A_SCHOOL_DAY = "NOT_A_SCHOOL_DAY"
    FUTURE_DATE = "FUTURE_DATE"
    NO_CURRENT_TERM = "NO_CURRENT_TERM"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _holidays(start: date, end: date) -> set[date]:
    return set(Holiday.objects.filter(date__range=(start, end)).values_list("date", flat=True))


def is_school_day(term: Term, d: date) -> bool:
    if not (term.start_date <= d <= term.end_date) or d.weekday() >= 5:
        return False
    return not Holiday.objects.filter(date=d).exists()


def school_days(term: Term, *, until: date | None = None) -> list[date]:
    end = min(term.end_date, until) if until else term.end_date
    if end < term.start_date:
        return []
    holidays = _holidays(term.start_date, end)
    out: list[date] = []
    d = term.start_date
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def check_register_date(d: date, *, term: Term | None = None) -> Term:
    """Return the current term if a register may be taken on ``d``, else raise.

    ``term``: the current term, when the caller has just loaded it (saves a query).
    """
    if term is None:
        term = current_term()
    if term is None:
        raise RegisterDateError(RegisterDateError.NO_CURRENT_TERM, "There is no current term.")
    if d > timezone.localdate():
        raise RegisterDateError(
            RegisterDateError.FUTURE_DATE, "A register cannot be taken for a future date."
        )
    if not is_school_day(term, d):
        raise RegisterDateError(
            RegisterDateError.NOT_A_SCHOOL_DAY, "That date is not a school day."
        )
    return term
