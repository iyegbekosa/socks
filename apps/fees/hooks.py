"""Post-commit hooks for the ledger."""

import logging

logger = logging.getLogger(__name__)


def on_payment_confirmed(payment_id: int) -> None:
    """Queue the receipt PDF (which then queues the guardian message). A failure to enqueue is
    logged by type and swallowed: the receipt stays without a PDF and "Regenerate" fixes it."""
    from apps.fees.tasks import generate_receipt_task

    try:
        generate_receipt_task.apply_async(args=[payment_id], queue="reports")
    except Exception as exc:  # noqa: BLE001 - a confirmed payment must never fail here
        logger.warning(
            "Could not queue the receipt for payment %s (%s).", payment_id, type(exc).__name__
        )
