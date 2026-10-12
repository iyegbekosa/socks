"""Post-commit hooks for the ledger. Task 4 fills ``on_payment_confirmed``."""


def on_payment_confirmed(payment_id: int) -> None:
    """Runs after a new Payment's transaction commits. A no-op for now."""
    return None
