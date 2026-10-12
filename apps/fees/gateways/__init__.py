"""Payment gateway adapter (FR-11): protocol, result types, errors and ``get_gateway()``.

Gateway messages are never shown to users: ``GatewayUnavailable`` maps to
``UNAVAILABLE_MESSAGE`` and ``GatewayRejected`` always carries a fixed, safe message.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

UNAVAILABLE_MESSAGE = "The payment provider is not responding. Please try again shortly."
RESOLVE_REJECTED_MESSAGE = "We could not find that account. Check the bank and account number."

# Keys dropped from stored gateway payloads: card and payer details (no PII kept).
PII_KEYS = frozenset({"customer", "authorization"})


class GatewayError(Exception):
    """Base class for gateway failures."""


class GatewayUnavailable(GatewayError):
    """Network error, timeout, 5xx, a non-JSON body or ``status: false``. Retry later."""


class GatewayRejected(GatewayError):
    """The gateway refused the input (e.g. an account that does not resolve)."""

    def __init__(self, message: str = RESOLVE_REJECTED_MESSAGE):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class InitResult:
    authorization_url: str


@dataclass(frozen=True)
class VerifyResult:
    status: str
    amount_kobo: int
    currency: str
    subaccount: str | None
    paid_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Bank:
    code: str
    name: str


class PaymentGateway(Protocol):
    def initialize(
        self,
        *,
        email: str,
        amount_kobo: int,
        reference: str,
        subaccount: str,
        callback_url: str,
        metadata: dict[str, Any],
    ) -> InitResult: ...

    def verify(self, reference: str) -> VerifyResult: ...

    def banks(self) -> list[Bank]: ...

    def resolve_account(self, account_number: str, bank_code: str) -> str: ...

    def save_subaccount(
        self, *, code: str | None, business_name: str, bank_code: str, account_number: str
    ) -> str: ...

    def check_signature(self, raw_body: bytes, header: str) -> bool: ...


def get_gateway() -> PaymentGateway:
    """An instance of ``settings.PAYMENT_GATEWAY``."""
    gateway: PaymentGateway = import_string(settings.PAYMENT_GATEWAY)()
    return gateway


def strip_pii(value: Any) -> Any:
    """``value`` with every ``customer``/``authorization`` key removed, at any depth."""
    if isinstance(value, dict):
        return {k: strip_pii(v) for k, v in value.items() if k not in PII_KEYS}
    if isinstance(value, list):
        return [strip_pii(v) for v in value]
    return value


def hmac_sha512(key: str, raw_body: bytes) -> str:
    return hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()


def signature_matches(key: str, raw_body: bytes, header: str | None) -> bool:
    """Constant-time check of an ``x-paystack-signature`` header. Missing header/key → False."""
    if not key or not header or not isinstance(header, str):
        return False
    expected = hmac_sha512(key, raw_body)
    return hmac.compare_digest(expected.encode(), header.strip().encode("utf-8"))
