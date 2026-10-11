import logging
import re

from pythonjsonlogger.json import JsonFormatter as _BaseJsonFormatter

from apps.core.request_id import request_id_var, role_var, schema_name_var, user_id_var

SENSITIVE_KEYS = frozenset({"password", "otp", "token", "secret", "card", "pin", "authorization"})
_SEGMENT_SPLIT = re.compile(r"[_\-.]")
REDACTED = "[REDACTED]"

_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)


def is_sensitive_key(key: str) -> bool:
    """True when a segment of the key (split on _ - .) is a sensitive word, digits allowed after."""
    for segment in _SEGMENT_SPLIT.split(key.lower()):
        if segment.rstrip("0123456789") in SENSITIVE_KEYS:
            return True
    return False


class JsonFormatter(_BaseJsonFormatter):
    """One JSON object per line."""


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.schema = schema_name_var.get()
        record.user_id = user_id_var.get()
        record.role = role_var.get()
        return True


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key in _STANDARD_ATTRS:
                continue
            if is_sensitive_key(key):
                setattr(record, key, REDACTED)
        return True
