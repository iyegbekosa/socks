"""Strict placeholder rendering for message bodies. Only plain ``{name}`` fields from a fixed
list are allowed: no attributes, indexing, conversions or format specs. Values are plain text."""

import string
from collections.abc import Iterable, Mapping
from typing import Any

from django.core.exceptions import ValidationError

PLACEHOLDERS = ("school_name", "student_first_name", "term", "balance", "date")
PER_STUDENT_ONLY = frozenset({"student_first_name", "balance"})


def allowed(per_student: bool) -> tuple[str, ...]:
    return tuple(p for p in PLACEHOLDERS if per_student or p not in PER_STUDENT_ONLY)


def _refuse(field: str, names: Iterable[str]) -> ValidationError:
    listed = ", ".join(f"{{{n}}}" for n in names)
    return ValidationError(f"Unknown placeholder {field}. Use only: {listed}", code="placeholder")


def _fields(body: str, names: tuple[str, ...]) -> list[str]:
    """The field names in ``body``, refusing anything but plain allowed ``{name}`` fields."""
    try:
        parsed = list(string.Formatter().parse(body))
    except ValueError as exc:
        brace = "}" if "'}'" in str(exc) and "expected" not in str(exc) else "{"
        raise _refuse(brace, names) from None
    out: list[str] = []
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in names or spec or conversion:
            shown = field + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "")
            raise _refuse(f"{{{shown}}}", names)
        out.append(field)
    return out


def literal(value: str) -> str:
    """``value`` as literal text inside a body: braces doubled so the strict parser never reads
    them as a placeholder. For senders that fill a non-placeholder value in before ``notify``."""
    return value.replace("{", "{{").replace("}", "}}")


def check_template(body: str, *, per_student: bool) -> None:
    """Raise ``ValidationError`` unless every placeholder is allowed for this kind of message."""
    _fields(body, allowed(per_student))


class _StrictFormatter(string.Formatter):
    def get_value(self, key: Any, args: Any, kwargs: Mapping[str, Any]) -> Any:
        if not isinstance(key, str) or key not in kwargs:
            raise _refuse(f"{{{key}}}", PLACEHOLDERS)
        return kwargs[key]

    def format_field(self, value: Any, format_spec: str) -> str:
        return str(value)


def render(body: str, ctx: dict[str, str]) -> str:
    """Fill ``body`` from ``ctx``. Refuses unknown, missing or non-plain placeholders."""
    _fields(body, PLACEHOLDERS)
    values = {k: str(v) for k, v in ctx.items() if k in PLACEHOLDERS}
    return _StrictFormatter().vformat(body, (), values)
