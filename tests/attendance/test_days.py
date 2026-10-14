from datetime import date
from unittest import mock

import pytest
from django_tenants.utils import schema_context

from apps.academics.models import Term
from apps.attendance import days
from apps.attendance.models import Holiday
from tests.fees.factories import make_terms

pytestmark = pytest.mark.django_db

TODAY = date(2026, 1, 12)  # a Monday inside the term


@pytest.fixture
def world(school_a):
    with schema_context("school_a"):
        w = make_terms()
        # Mon 5 Jan to Fri 16 Jan 2026 is the current term; the other is a past term.
        Term.objects.filter(pk=w.term2.pk).update(
            start_date=date(2025, 9, 1), end_date=date(2025, 12, 15), is_current=False
        )
        Term.objects.filter(pk=w.term1.pk).update(
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 16), is_current=True
        )
        w.term1.refresh_from_db()
        Holiday.objects.create(date=date(2026, 1, 7), name="Founders day")
        yield w


def _today(d=TODAY):
    return mock.patch("apps.attendance.days.timezone.localdate", return_value=d)


def test_school_days_skip_weekends_and_holidays(world):
    got = days.school_days(world.term1)
    assert len(got) == 9
    assert date(2026, 1, 7) not in got
    assert date(2026, 1, 10) not in got
    assert got[0] == date(2026, 1, 5) and got[-1] == date(2026, 1, 16)


def test_school_days_until_caps(world):
    got = days.school_days(world.term1, until=date(2026, 1, 8))
    assert got == [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 8)]


def test_is_school_day(world):
    assert days.is_school_day(world.term1, date(2026, 1, 6))
    assert not days.is_school_day(world.term1, date(2026, 1, 7))
    assert not days.is_school_day(world.term1, date(2026, 1, 4))


def test_weekend_and_holiday_refused(world):
    with _today():
        for d in (date(2026, 1, 10), date(2026, 1, 7)):
            with pytest.raises(days.RegisterDateError) as exc:
                days.check_register_date(d)
            assert exc.value.code == "NOT_A_SCHOOL_DAY"


def test_valid_date_returns_current_term(world):
    with _today():
        assert days.check_register_date(date(2026, 1, 6)) == world.term1


def test_future_date_refused(world):
    with _today():
        with pytest.raises(days.RegisterDateError) as exc:
            days.check_register_date(date(2026, 1, 13))
    assert exc.value.code == "FUTURE_DATE"


def test_past_term_date_refused(world):
    with _today():
        with pytest.raises(days.RegisterDateError) as exc:
            days.check_register_date(date(2025, 10, 6))
    assert exc.value.code == "NOT_A_SCHOOL_DAY"


def test_no_current_term(world):
    Term.objects.update(is_current=False)
    with _today():
        with pytest.raises(days.RegisterDateError) as exc:
            days.check_register_date(date(2026, 1, 6))
    assert exc.value.code == "NO_CURRENT_TERM"
