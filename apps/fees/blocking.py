"""Results withheld while fees are owed (UC-17).

Debt attribution: a payment clears the oldest debt first, so a later invoice's balance covers
its ``brought_forward`` part before its own fees. A term's debt therefore survives being
carried into a later invoice until that brought-forward amount is paid.

Only student-facing paths call ``results_withheld``; staff views never do.
"""

from django.db.models import F, Q, Sum

from apps.academics.models import Term
from apps.fees.invoicing import _earlier_terms_q, outstanding_for
from apps.fees.models import FeeSettings, Invoice, InvoiceLine
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
    """What the student still owes for ``term`` and earlier terms, in kobo.

    The non-carried balances up to and including ``term``, plus, for each later non-carried
    invoice, the part of its balance that is balance brought forward.
    """
    owed = outstanding_for(student, up_to_term=term)
    later = (
        Invoice.objects.filter(student=student)
        .exclude(status=Invoice.CARRIED)
        .exclude(_earlier_terms_q(term, inclusive=True))
        .filter(total_kobo__gt=F("paid_kobo"))
        .annotate(
            carried_in=Sum("lines__amount_kobo", filter=Q(lines__kind=InvoiceLine.BROUGHT_FORWARD))
        )
    )
    for invoice in later:
        owed += max(0, min(invoice.balance_kobo, invoice.carried_in or 0))
    return owed


def results_withheld(student: Student, term: Term) -> bool:
    """True when results blocking is on and the student owes anything for ``term`` or before."""
    if not blocking_enabled():
        return False
    return owed_up_to(student, term) > 0
