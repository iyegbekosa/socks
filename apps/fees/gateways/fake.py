"""A stand-in gateway for DEBUG and tests. Refuses to exist when DEBUG is off.

State lives in Django's cache under ``fees:fakegw:{schema}:{reference}`` for one day.
``verify`` reports ``pending`` until ``record_result`` stores an outcome (Task 8's fake
checkout buttons, or tests).

``initialize`` returns the ``fees:fake_checkout`` URL. There is no request object in a
gateway call, so the host comes from ``callback_url``: when the caller passes an absolute
callback (Task 7 does, built from the current request), the checkout URL uses the same
scheme and host; otherwise it is relative.
"""

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.fees.gateways import (
    Bank,
    GatewayRejected,
    InitResult,
    VerifyResult,
    hmac_sha512,
    signature_matches,
)

STATE_SECONDS = 24 * 60 * 60
FAKE_BANKS = [
    Bank(code="044", name="Access Bank (test)"),
    Bank(code="058", name="GTBank (test)"),
    Bank(code="057", name="Zenith Bank (test)"),
]
# Resolving this account number fails, to exercise the rejection path by hand.
REJECTED_ACCOUNT = "0000000000"


def _require_debug() -> None:
    if not settings.DEBUG:
        raise ImproperlyConfigured("FakeGateway is only available when DEBUG is on.")


def _key(reference: str) -> str:
    return f"fees:fakegw:{getattr(connection, 'schema_name', '-')}:{reference}"


def _signing_key() -> str:
    return settings.PAYSTACK_SECRET_KEY or "fake"


class FakeGateway:
    def __init__(self) -> None:
        _require_debug()

    # --- state helpers (Task 8 and tests) ------------------------------------

    @staticmethod
    def lookup(reference: str) -> dict[str, Any] | None:
        """The recorded state for ``reference`` in the current schema, or None."""
        _require_debug()
        state = cache.get(_key(reference))
        return dict(state) if isinstance(state, dict) else None

    @staticmethod
    def record_result(reference: str, status: str, amount_kobo: int | None = None) -> None:
        """Store the checkout outcome. ``amount_kobo`` overrides the initialized amount."""
        _require_debug()
        state = FakeGateway.lookup(reference) or {"amount_kobo": 0, "subaccount": None}
        state["status"] = status
        if amount_kobo is not None:
            state["amount_kobo"] = int(amount_kobo)
        state["paid_at"] = timezone.now().isoformat() if status == "success" else None
        cache.set(_key(reference), state, STATE_SECONDS)

    @staticmethod
    def sign(raw_body: bytes) -> str:
        """The signature header value ``check_signature`` accepts (for Task 8's fake webhook)."""
        return hmac_sha512(_signing_key(), raw_body)

    # --- PaymentGateway ---------------------------------------------------------

    def initialize(
        self,
        *,
        email: str,
        amount_kobo: int,
        reference: str,
        subaccount: str,
        callback_url: str,
        metadata: dict[str, Any],
    ) -> InitResult:
        cache.set(
            _key(reference),
            {
                "status": "pending",
                "amount_kobo": int(amount_kobo),
                "subaccount": subaccount,
                "callback_url": callback_url,
                "metadata": dict(metadata),
                "paid_at": None,
            },
            STATE_SECONDS,
        )
        path = reverse("fees:fake_checkout", args=[reference])
        parts = urlsplit(callback_url or "")
        if parts.scheme in ("http", "https") and parts.netloc:
            return InitResult(authorization_url=f"{parts.scheme}://{parts.netloc}{path}")
        return InitResult(authorization_url=path)

    def verify(self, reference: str) -> VerifyResult:
        state = self.lookup(reference) or {}
        paid_at = state.get("paid_at")
        status = state.get("status") or "pending"
        return VerifyResult(
            status=status,
            amount_kobo=int(state.get("amount_kobo") or 0),
            currency="NGN",
            subaccount=state.get("subaccount"),
            paid_at=datetime.fromisoformat(paid_at) if paid_at else None,
            raw={
                "reference": reference,
                "status": status,
                "amount": int(state.get("amount_kobo") or 0),
                "currency": "NGN",
                "metadata": state.get("metadata") or {},
            },
        )

    def banks(self) -> list[Bank]:
        return list(FAKE_BANKS)

    def resolve_account(self, account_number: str, bank_code: str) -> str:
        if account_number == REJECTED_ACCOUNT or bank_code not in {b.code for b in FAKE_BANKS}:
            raise GatewayRejected()
        return "TEST ACCOUNT"

    def save_subaccount(
        self, *, code: str | None, business_name: str, bank_code: str, account_number: str
    ) -> str:
        return code or f"ACCT_FAKE{bank_code}{account_number[-4:]}"

    def check_signature(self, raw_body: bytes, header: str) -> bool:
        return signature_matches(_signing_key(), raw_body, header)
