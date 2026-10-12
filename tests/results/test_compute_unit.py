"""Pure computation helpers: no database."""

from decimal import Decimal

import pytest

from apps.results.compute import (
    competition_rank,
    grade_lookup,
    ordinal,
    round_avg,
    stats,
    subject_total,
)
from apps.results.seeding import BASIC_SCALE


def test_subject_total_keeps_one_decimal():
    assert subject_total([Decimal("30"), Decimal("50")]) == Decimal("80.0")
    assert str(subject_total([Decimal("12.5"), Decimal("40.3")])) == "52.8"
    assert str(subject_total([])) == "0.0"


def test_competition_rank_ties_share_and_skip():
    assert competition_rank({"a": Decimal(80), "b": Decimal(80), "c": Decimal(60)}) == {
        "a": 1,
        "b": 1,
        "c": 3,
    }
    assert competition_rank(
        {"a": Decimal(98), "b": Decimal(80), "c": Decimal(80), "d": Decimal(60)}
    ) == {"a": 1, "b": 2, "c": 2, "d": 4}
    assert competition_rank({}) == {}


def test_round_avg_half_up_two_places():
    assert round_avg(Decimal(230), 3) == Decimal("76.67")
    assert round_avg(Decimal("119"), 3) == Decimal("39.67")
    assert str(round_avg(Decimal("373"), 5)) == "74.60"
    # 0.125 rounds up (half-up), not to even.
    assert round_avg(Decimal("0.25"), 2) == Decimal("0.13")
    with pytest.raises(ValueError):
        round_avg(Decimal(1), 0)


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (23, "23rd"),
        (101, "101st"),
        (111, "111th"),
        (112, "112th"),
        (113, "113th"),
    ],
)
def test_ordinal(n, text):
    assert ordinal(n) == text


def test_stats():
    totals = [Decimal(v) for v in (80, 80, 60, 55, 30, 98)]
    assert stats(totals) == (Decimal(98), Decimal(30), Decimal("67.17"))


def test_grade_lookup_rounds_half_up_to_whole_number():
    grade = grade_lookup(BASIC_SCALE)
    assert grade(Decimal("69.5")) == ("A", "Excellent")
    assert grade(Decimal("69.4")) == ("B", "Very good")
    assert grade(Decimal("44")) == ("E", "Poor")
    assert grade(Decimal("0")) == ("F", "Fail")
