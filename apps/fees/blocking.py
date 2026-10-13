"""Results withheld while fees are owed (UC-17).

Debt attribution (Ruling G'): payments clear the oldest debt first. When a student's latest
invoice up to term T was carried forward, its balance (frozen at carry) is the debt that left
terms <= T; each later invoice's payments pay it down, in term order, before anything newer.

Only student-facing paths call ``results_withheld``; staff views never do.
"""

from django.db.models import BooleanField, Case, Value, When

from apps.academics.models import Term
from apps.fees.invoicing import _earlier_terms_q
from apps.fees.models import FeeSettings, Invoice
from apps.students.models import Student

WITHHELD_MESSAGE = (
    "Your results for this term are withheld until outstanding fees are paid. "
    "Please contact the school bursar."
)


def blocking_enabled() -> bool:
    """``FeeSettings.block_results_when_owing``, without creating the settings row."""
    return bool(
        FeeSettings.objects.filter(pk=1).values_list("block_results_when_owing", flat=True).first()
    )


def owed_up_to(student: Student, term: Term) -> int:
    """What the student still owes for ``term`` and earlier terms, in kobo (one query).

    ``outstanding_for(student, up_to_term=term)`` (non-carried balances up to ``term``) plus D:
    if the latest invoice up to ``term`` was carried, D starts at its balance and each later
    invoice's ``paid_kobo`` reduces it, oldest first, never below 0. Otherwise D is 0.
    """
    invoices = (
        Invoice.objects.filter(student=student)
        .annotate(
            up_to=Case(
                When(_earlier_terms_q(term, inclusive=True), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        .order_by("term__session__start_date", "term__number", "pk")
    )
    outstanding = 0
    latest: Invoice | None = None
    later: list[Invoice] = []
    for invoice in invoices:
        if invoice.up_to:  # type: ignore[attr-defined]
            if invoice.status != Invoice.CARRIED:
                outstanding += invoice.balance_kobo
            latest = invoice
        else:
            later.append(invoice)
    left = latest.balance_kobo if latest is not None and latest.status == Invoice.CARRIED else 0
    for newer in later:
        if left <= 0:
            break
        left = max(0, left - newer.paid_kobo)
    return outstanding + max(0, left)


def results_withheld(student: Student, term: Term) -> bool:
    """True when results blocking is on and the student owes anything for ``term`` or before."""
    if not blocking_enabled():
        return False
    return owed_up_to(student, term) > 0
