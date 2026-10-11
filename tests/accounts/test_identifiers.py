import pytest

from apps.accounts.identifiers import (
    classify_identifier,
    normalise_admission_number,
    normalise_ng_phone,
)


@pytest.mark.parametrize(
    "raw", ["0803 123 4567", "+234 803 123 4567", "2348031234567", "08031234567"]
)
def test_phone_normalises(raw):
    assert normalise_ng_phone(raw) == "+2348031234567"


@pytest.mark.parametrize("raw", ["0803123456", "+44 7700 900123", "01-2345678", ""])
def test_phone_rejects(raw):
    with pytest.raises(ValueError):
        normalise_ng_phone(raw)


def test_classify():
    assert classify_identifier("Ada@School.NG") == ("email", "ada@school.ng")
    assert classify_identifier("0803 123 4567") == ("phone", "+2348031234567")
    assert classify_identifier(" gfs/2026/0042 ") == ("admission", "GFS/2026/0042")


def test_admission_normalise():
    assert normalise_admission_number(" gfs/2026/0042 ") == "GFS/2026/0042"
