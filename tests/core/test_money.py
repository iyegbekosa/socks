from decimal import Decimal

import pytest

from apps.core.money import kobo_from_naira, money_fields, naira_display


def test_naira_display():
    assert naira_display(15_000_000) == "₦150,000.00"
    assert naira_display(5) == "₦0.05"
    assert naira_display(-250_00) == "-₦250.00"


def test_kobo_from_naira():
    assert kobo_from_naira("150000") == 15_000_000
    assert kobo_from_naira("0.1") == 10
    assert kobo_from_naira(12) == 1200
    assert kobo_from_naira(Decimal("1.25")) == 125


def test_float_rejected():
    with pytest.raises(TypeError):
        kobo_from_naira(0.1)


@pytest.mark.parametrize("value", ["1.005", Decimal("0.001"), "abc", "NaN", "Infinity"])
def test_invalid_values_rejected(value):
    with pytest.raises(ValueError):
        kobo_from_naira(value)


def test_money_fields():
    assert money_fields(15_000_000) == {
        "amount_kobo": 15_000_000,
        "amount_display": "₦150,000.00",
    }
