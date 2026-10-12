from apps.core.tasks import TenantTask
from apps.fees.models import Receipt
from apps.fees.receipts import generate_receipt, send_receipt
from config.celery import app


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
