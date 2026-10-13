import logging

from django_tenants.utils import get_public_schema_name

from apps.core.tasks import TenantTask
from apps.fees.confirm import process_attempt
from apps.fees.gateways import GatewayUnavailable
from apps.fees.receipts import FIRST_STORED, send_receipt, store_receipt
from apps.fees.reconcile import reverify_school
from apps.tenancy.models import School
from config.celery import app

logger = logging.getLogger(__name__)

# Seconds before each retry of a verification the gateway could not answer.
VERIFY_RETRY_COUNTDOWNS = (60, 300, 900)


@app.task(base=TenantTask, queue="reports")
def generate_receipt_task(payment_id: int) -> None:
    """Render and store a payment's receipt PDF (FR-14), then queue the guardian message when
    this run produced the first PDF. Args are redacted from Celery logs (``SECRET_ARG_TASKS``).

    "First" is decided by a conditional update in ``store_receipt``, so two racing runs message
    guardians once. A failure to enqueue the message is logged by type and swallowed: the PDF
    is already stored."""
    if store_receipt(payment_id) != FIRST_STORED:
        return
    try:
        send_receipt_task.apply_async(args=[payment_id], queue="notifications")
    except Exception as exc:  # noqa: BLE001 - the stored PDF must not be undone by a broker
        logger.warning(
            "Could not queue the receipt message for payment %s (%s).",
            payment_id,
            type(exc).__name__,
        )


@app.task(base=TenantTask, queue="notifications")
def send_receipt_task(payment_id: int) -> None:
    """Tell the guardians a payment was received; see ``receipts.send_receipt``."""
    send_receipt(payment_id)


@app.task(base=TenantTask, bind=True, queue="payments", max_retries=len(VERIFY_RETRY_COUNTDOWNS))
def process_attempt_task(self, reference: str) -> str:
    """Verify and settle one online payment attempt (``confirm.process_attempt``). Enqueued by
    the public webhook with an explicit ``schema_name`` header; a gateway outage retries after
    60 s, 5 min and 15 min, carrying the schema (``TenantTask.retry``)."""
    try:
        return process_attempt(reference)
    except GatewayUnavailable as exc:
        step = min(self.request.retries, len(VERIFY_RETRY_COUNTDOWNS) - 1)
        raise self.retry(exc=exc, countdown=VERIFY_RETRY_COUNTDOWNS[step]) from None


@app.task(base=TenantTask, queue="payments")
def reverify_school_task() -> dict[str, int]:
    """Re-verify this school's open payment attempts (``reconcile.reverify_school``)."""
    return reverify_school()


@app.task(queue="payments")
def reverify_all_schools() -> int:
    """Beat entry (every 15 minutes), run in the public schema: queue one re-verification per
    active, fully provisioned school, with an explicit schema header. Returns the count."""
    schools = School.objects.filter(is_active=True, provisioning_status="ready").exclude(
        schema_name=get_public_schema_name()
    )
    count = 0
    for school in schools:
        reverify_school_task.apply_async(
            queue="payments", headers={"schema_name": school.schema_name}
        )
        count += 1
    return count
