from apps.core.tasks import TenantTask
from apps.fees.confirm import process_attempt
from apps.fees.gateways import GatewayUnavailable
from apps.fees.models import Receipt
from apps.fees.receipts import generate_receipt, send_receipt
from config.celery import app

# Seconds before each retry of a verification the gateway could not answer.
VERIFY_RETRY_COUNTDOWNS = (60, 300, 900)


@app.task(base=TenantTask, queue="reports")
def generate_receipt_task(payment_id: int) -> None:
    """Render and store a payment's receipt PDF (FR-14), then queue the guardian message when
    this run produced the first PDF. Args are redacted from Celery logs (``SECRET_ARG_TASKS``)."""
    had_pdf = Receipt.objects.filter(payment_id=payment_id).exclude(pdf_name="").exists()
    if generate_receipt(payment_id) and not had_pdf:
        send_receipt_task.apply_async(args=[payment_id], queue="notifications")


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
