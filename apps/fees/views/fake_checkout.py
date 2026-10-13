"""DEBUG-only stand-in for Paystack's hosted checkout (404 when DEBUG is off).

"Pay (test)" and "Fail (test)" record the outcome in the FakeGateway, then hand a correctly
signed ``charge.success`` / ``charge.failed`` body to the real public webhook handler in
process (no HTTP call to ourselves). The handler runs all of its checks, and with eager Celery
the payment is verified and confirmed before the redirect to the return page.
"""

import json
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.fees import webhooks
from apps.fees.models import PaymentAttempt

OUTCOMES = {"success": "charge.success", "failed": "charge.failed"}


def _deliver_webhook(reference: str, outcome: str, amount_kobo: int) -> None:
    from apps.fees.gateways.fake import FakeGateway

    body = json.dumps(
        {
            "event": OUTCOMES[outcome],
            "data": {
                "reference": reference,
                "status": outcome,
                "amount": amount_kobo,
                "currency": "NGN",
            },
        }
    ).encode()
    hook = HttpRequest()
    hook.method = "POST"
    hook.path = hook.path_info = webhooks.WEBHOOK_PATH
    hook.META = {
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_X_PAYSTACK_SIGNATURE": FakeGateway.sign(body),
    }
    hook._body = body
    webhooks.paystack_webhook(hook)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def fake_checkout(request, reference: str):
    if not settings.DEBUG:
        raise Http404
    from apps.fees.gateways.fake import FakeGateway

    state = FakeGateway.lookup(reference)
    if state is None:
        raise Http404

    if request.method == "POST":
        attempt = PaymentAttempt.objects.filter(
            reference=reference, student__user=request.user
        ).first()
        if attempt is None:
            raise Http404
        outcome = request.POST.get("outcome", "")
        if outcome not in OUTCOMES:
            return HttpResponseBadRequest()
        FakeGateway.record_result(reference, outcome, amount_kobo=attempt.amount_kobo)
        _deliver_webhook(reference, outcome, attempt.amount_kobo)
        return redirect(f"{reverse('fees:pay_return')}?{urlencode({'reference': reference})}")

    return render(
        request,
        "fees/fake_checkout.html",
        {"reference": reference, "amount_kobo": state.get("amount_kobo") or 0, "state": state},
    )
