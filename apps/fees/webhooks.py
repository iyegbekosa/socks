"""The public Paystack webhook (§4.5 step 4). Served by the public URLconf only.

It checks the size and the HMAC signature of the raw body, reads ``data.reference`` from a
``charge.success`` event, finds the school through the public ``PaymentRoute`` and enqueues
``process_attempt_task`` there. It never reads or writes tenant tables and never confirms
anything itself: confirmation needs the server-side ``verify`` in the task.

Logs carry the reference at most: never the body, emails or amounts.
"""

import json
import logging

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_tenants.utils import get_public_schema_name, schema_context

from apps.fees.gateways import get_gateway
from apps.fees.tasks import process_attempt_task
from apps.tenancy.models import PaymentRoute

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/payments/webhook/paystack/"
MAX_BODY_BYTES = 64 * 1024
SIGNATURE_HEADER = "x-paystack-signature"
CHARGE_SUCCESS = "charge.success"
MAX_REFERENCE_LENGTH = 40


def _declared_length(request: HttpRequest) -> int:
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return 0


def _reference(payload: object) -> str | None:
    """``data.reference`` of a ``charge.success`` event, or None for anything else."""
    if not isinstance(payload, dict) or payload.get("event") != CHARGE_SUCCESS:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    reference = data.get("reference")
    if not isinstance(reference, str) or not 0 < len(reference) <= MAX_REFERENCE_LENGTH:
        return None
    return reference


@csrf_exempt
@require_POST
def paystack_webhook(request: HttpRequest) -> HttpResponse:
    if _declared_length(request) > MAX_BODY_BYTES:
        return HttpResponse(status=413)
    raw = request.body
    if len(raw) > MAX_BODY_BYTES:
        return HttpResponse(status=413)

    if not get_gateway().check_signature(raw, request.headers.get(SIGNATURE_HEADER, "")):
        logger.warning("Paystack webhook refused: bad signature.")
        return HttpResponse(status=401)

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        logger.warning("Paystack webhook ignored: body is not JSON.")
        return HttpResponse(status=400)

    reference = _reference(payload)
    if reference is None:
        return HttpResponse(status=200)  # other events (and malformed ones) are not ours

    with schema_context(get_public_schema_name()):
        route = PaymentRoute.objects.filter(reference=reference).first()
        if route is None:
            logger.info("Paystack webhook ignored: unknown reference.")
            return HttpResponse(status=200)
        try:
            process_attempt_task.apply_async(
                args=[reference], queue="payments", headers={"schema_name": route.schema_name}
            )
        except Exception as exc:  # noqa: BLE001 - re-verification is the safety net
            logger.warning(
                "Paystack webhook for %s could not be queued (%s).",
                reference,
                type(exc).__name__,
            )
    return HttpResponse(status=200)
