"""SMS sending: ``send_sms`` enqueues ``deliver_sms``, which calls the configured backend."""

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from apps.core.tasks import TenantTask
from config.celery import app


@dataclass(frozen=True)
class SmsMessage:
    to: str
    body: str


outbox: list[SmsMessage] = []


def mask_phone(to: str) -> str:
    return f"***{to[-4:]}"


class ConsoleSmsBackend:
    """Development only. Prints (not logs) so the code is visible locally, like Django's
    console email backend; OTPs must never reach the structured logs (§11.1)."""

    def send(self, to: str, body: str) -> None:
        print(f"[SMS to {mask_phone(to)}] {body}", flush=True)  # noqa: T201


class LocmemSmsBackend:
    def send(self, to: str, body: str) -> None:
        outbox.append(SmsMessage(to, body))


def _backend_path() -> str:
    path = getattr(settings, "SMS_BACKEND", None)
    if not path:
        raise ImproperlyConfigured("SMS_BACKEND is not set and DEBUG is off.")
    return path


@app.task(base=TenantTask, queue="notifications")
def deliver_sms(to: str, body: str) -> None:
    import_string(_backend_path())().send(to, body)


def send_sms(to: str, body: str) -> None:
    _backend_path()  # fail at the call site, not in a worker
    deliver_sms.apply_async(args=[to, body])
