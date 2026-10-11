"""Normalisation of the three login identifiers: email, Nigerian phone, admission number."""

import re
from typing import Literal

_NG_LOCAL = re.compile(r"^0([789][01]\d{8})$")
_NG_INTL = re.compile(r"^234([789][01]\d{8})$")


def normalise_ng_phone(raw: str) -> str:
    """Return +234 E.164 form of a Nigerian mobile number or raise ValueError."""
    cleaned = re.sub(r"[\s\-().]", "", raw or "")
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
        match = _NG_INTL.match(cleaned)
    elif cleaned.startswith("234"):
        match = _NG_INTL.match(cleaned)
    else:
        match = _NG_LOCAL.match(cleaned)
    if not match:
        raise ValueError("Enter a valid Nigerian mobile number.")
    return f"+234{match.group(1)}"


def looks_like_phone(raw: str) -> bool:
    """True when login would treat this identifier as a Nigerian phone number."""
    try:
        normalise_ng_phone(raw)
    except ValueError:
        return False
    return True


def normalise_admission_number(raw: str) -> str:
    return (raw or "").strip().upper()


def classify_identifier(raw: str) -> tuple[Literal["email", "phone", "admission"], str]:
    value = (raw or "").strip()
    if "@" in value:
        return "email", value.lower()
    try:
        return "phone", normalise_ng_phone(value)
    except ValueError:
        return "admission", normalise_admission_number(value)
