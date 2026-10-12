import pytest

from apps.core.parsing import parse_id


def test_parse_id_valid():
    assert parse_id("12") == 12


@pytest.mark.parametrize("raw", ["", "²", "-1", "1.0", " 1", "1" * 19, "1" * 5000])
def test_parse_id_rejects(raw):
    assert parse_id(raw) is None
