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
