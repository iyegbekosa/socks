"""Paystack client over httpx. Never logs or surfaces gateway response bodies."""

import logging
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.utils.dateparse import parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context

from apps.fees.gateways import (
    Bank,
    GatewayRejected,
    GatewayUnavailable,
    InitResult,
    VerifyResult,
    signature_matches,
    strip_pii,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.paystack.co"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# Global, not per school: the bank list is the same for every tenant. The cache's
# KEY_FUNCTION prefixes keys with the active schema, so it is read and written under the
# public schema (cache calls only; no query runs inside that block).
BANKS_CACHE_KEY = "fees:paystack:banks:v1"
BANKS_CACHE_SECONDS = 24 * 60 * 60


class PaystackGateway:
    def __init__(self, client: httpx.Client | None = None):
        key = settings.PAYSTACK_SECRET_KEY
        if not key:
            raise ImproperlyConfigured("PAYSTACK_SECRET_KEY is required for PaystackGateway.")
        self._key: str = key
        self._client = client

    # --- transport ------------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "params": params,
            "json": json,
            "headers": {"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            "timeout": TIMEOUT,
        }
        url = BASE_URL + path
        try:
            if self._client is not None:
                return self._client.request(method, url, **kwargs)
            with httpx.Client() as client:
                return client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("paystack %s failed: %s", path.split("?")[0], type(exc).__name__)
            raise GatewayUnavailable(type(exc).__name__) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _data(self, method: str, path: str, **kwargs: Any) -> Any:
        """``data`` of a successful call; anything else is ``GatewayUnavailable``."""
        response = self._send(method, path, **kwargs)
        if response.status_code >= 500:
            raise GatewayUnavailable(f"HTTP {response.status_code}")
        body = self._json(response)
        if body is None:
            raise GatewayUnavailable("non-JSON response")
        if body.get("status") is not True:
            raise GatewayUnavailable(f"status false (HTTP {response.status_code})")
        return body.get("data")

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
        data = self._data(
            "POST",
            "/transaction/initialize",
            json={
                "email": email,
                "amount": int(amount_kobo),
                "reference": reference,
                "currency": "NGN",
                "subaccount": subaccount,
                "bearer": "subaccount",
                "callback_url": callback_url,
                "metadata": metadata,
            },
        )
        url = data.get("authorization_url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise GatewayUnavailable("missing authorization_url")
        return InitResult(authorization_url=url)

    def verify(self, reference: str) -> VerifyResult:
        data = self._data("GET", "/transaction/verify/" + quote(reference, safe=""))
        if not isinstance(data, dict):
            raise GatewayUnavailable("missing data")
        status, amount, currency = data.get("status"), data.get("amount"), data.get("currency")
        if (
            not isinstance(status, str)
            or isinstance(amount, bool)
            or not isinstance(amount, int)
            or not isinstance(currency, str)
        ):
            raise GatewayUnavailable("malformed verify data")
        sub = data.get("subaccount")
        sub_code = sub.get("subaccount_code") if isinstance(sub, dict) else None
        paid_at_raw = data.get("paid_at") or data.get("paidAt")
        paid_at = parse_datetime(paid_at_raw) if isinstance(paid_at_raw, str) else None
        return VerifyResult(
            status=status,
            amount_kobo=amount,
            currency=currency,
            subaccount=sub_code if isinstance(sub_code, str) and sub_code else None,
            paid_at=paid_at,
            raw=strip_pii(data),
        )

    def banks(self) -> list[Bank]:
        with schema_context(get_public_schema_name()):
            cached = cache.get(BANKS_CACHE_KEY)
        if cached is not None:
            return [Bank(code=code, name=name) for code, name in cached]
        data = self._data("GET", "/bank", params={"country": "nigeria", "currency": "NGN"})
        if not isinstance(data, list):
            raise GatewayUnavailable("malformed bank list")
        pairs = [
            (str(row["code"]), str(row["name"]))
            for row in data
            if isinstance(row, dict) and row.get("code") and row.get("name")
        ]
        with schema_context(get_public_schema_name()):
            cache.set(BANKS_CACHE_KEY, pairs, BANKS_CACHE_SECONDS)
        return [Bank(code=code, name=name) for code, name in pairs]

    def resolve_account(self, account_number: str, bank_code: str) -> str:
        response = self._send(
            "GET",
            "/bank/resolve",
            params={"account_number": account_number, "bank_code": bank_code},
        )
        if response.status_code >= 500:
            raise GatewayUnavailable(f"HTTP {response.status_code}")
        if 400 <= response.status_code < 500:
            raise GatewayRejected()
        body = self._json(response)
        if body is None:
            raise GatewayUnavailable("non-JSON response")
        if body.get("status") is not True:
            raise GatewayRejected()
        data = body.get("data")
        name = data.get("account_name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise GatewayRejected()
        return name.strip()

    def save_subaccount(
        self, *, code: str | None, business_name: str, bank_code: str, account_number: str
    ) -> str:
        payload = {
            "business_name": business_name,
            "settlement_bank": bank_code,
            "account_number": account_number,
            "percentage_charge": 0,
        }
        if code:
            data = self._data("PUT", "/subaccount/" + quote(code, safe=""), json=payload)
        else:
            data = self._data("POST", "/subaccount", json=payload)
        new_code = data.get("subaccount_code") if isinstance(data, dict) else None
        if isinstance(new_code, str) and new_code:
            return new_code
        if code:
            return code
        raise GatewayUnavailable("missing subaccount_code")

    def check_signature(self, raw_body: bytes, header: str) -> bool:
        return signature_matches(self._key, raw_body, header)
