import logging

from pythonjsonlogger.json import JsonFormatter as _BaseJsonFormatter

from apps.core.request_id import request_id_var, role_var, schema_name_var, user_id_var

SENSITIVE_KEYS = ("password", "otp", "token", "secret", "card", "pin", "authorization")
REDACTED = "[REDACTED]"

_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)


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
            if any(word in key.lower() for word in SENSITIVE_KEYS):
                setattr(record, key, REDACTED)
        return True
