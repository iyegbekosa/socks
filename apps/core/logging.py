import logging
import re
import sys

from pythonjsonlogger.json import JsonFormatter as _BaseJsonFormatter

from apps.core.request_id import request_id_var, role_var, schema_name_var, user_id_var

SENSITIVE_KEYS = frozenset({"password", "otp", "token", "secret", "card", "pin", "authorization"})
_SEGMENT_SPLIT = re.compile(r"[_\-.]")
REDACTED = "[REDACTED]"
# Celery tasks whose arguments are secret (phone numbers, OTP codes). Celery's trace and
# worker loggers attach the task context as ``extra={"data": {...}}``, args included.
SECRET_ARG_TASKS = frozenset(
    {
        "apps.core.sms.deliver_sms",
        "apps.accounts.tasks.send_password_reset",
        "apps.results.tasks.notify_published_task",
        "apps.fees.tasks.generate_receipt_task",
        "apps.fees.tasks.send_receipt_task",
        "apps.fees.tasks.process_attempt_task",
        "apps.attendance.tasks.send_absence_emails_task",
        "apps.comms.tasks.send_email_batch_task",
        "apps.comms.tasks.send_held_school_task",
        "apps.comms.tasks.send_held_all_schools",
        "apps.comms.tasks.send_reminders_school_task",
        "apps.comms.tasks.send_reminders_all_schools",
    }
)
_TASK_ARG_KEYS = ("args", "kwargs", "argsrepr", "kwargsrepr")

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
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data.get("name") in SECRET_ARG_TASKS:
            # A copy: the same dict is Celery's message context and may be reused elsewhere.
            redacted = {k: (REDACTED if k in _TASK_ARG_KEYS else v) for k, v in data.items()}
            record.data = redacted
            if record.args is data:  # trace logs pass the context as the %-format args too
                record.args = redacted
        return True


class RedactedError(Exception):
    """Stands in for a caught exception in a logged traceback: its type name, never its text
    (exception messages can carry children's data)."""


def log_exception_redacted(log: logging.Logger, msg: str, *args: object) -> None:
    """``log.exception`` for the exception being handled, keeping the traceback frames and the
    exception's type but dropping its message, args and chained exceptions."""
    etype, _value, tb = sys.exc_info()
    name = etype.__name__ if etype is not None else "Exception"
    log.exception(msg + " (%s)", *args, name, exc_info=(RedactedError, RedactedError(name), tb))
