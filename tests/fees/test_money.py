import pytest

from apps.core.money import kobo_from_naira


def test_cap_is_inclusive():
    assert kobo_from_naira("100000000.00") == 10_000_000_000


def test_above_cap_is_rejected():
    with pytest.raises(ValueError, match="Amount is too large."):
        kobo_from_naira("100000000.01")


def test_negative_is_rejected():
    with pytest.raises(ValueError, match="Amount cannot be negative."):
        kobo_from_naira("-1")


def test_zero_is_allowed():
    assert kobo_from_naira("0") == 0


@pytest.mark.parametrize(
    "raw, kobo",
    [
        ("1,250.00", 125_000),
        ("₦1,250", 125_000),
        ("12,345,678.5", 1_234_567_850),
        ("999", 99_900),
        ("1250.00", 125_000),
    ],
)
def test_parse_naira_accepts_thousands_grouping(raw, kobo):
    from apps.fees.forms import parse_naira

    assert parse_naira(raw) == kobo


@pytest.mark.parametrize("raw", ["12,50", "5,00", "1,2345", ",100", "1,000,00", "1,250.", "1,,000"])
def test_parse_naira_refuses_commas_that_are_not_grouping(raw):
    from django.core.exceptions import ValidationError

    from apps.fees.forms import parse_naira

    with pytest.raises(ValidationError, match="must be a number in naira"):
        parse_naira(raw)
