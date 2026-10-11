from datetime import date, datetime

from django.template import Context, Template
from django.utils import formats, timezone


def test_date_format_is_day_first():
    assert formats.date_format(date(2026, 10, 11)) == "11/10/2026"


def test_date_filter_renders_day_first():
    out = Template("{{ d|date }}").render(Context({"d": date(2026, 10, 11)}))
    assert out == "11/10/2026"


def test_datetime_format():
    value = timezone.make_aware(datetime(2026, 10, 11, 14, 5))
    assert formats.date_format(value, "DATETIME_FORMAT") == "11/10/2026 14:05"
