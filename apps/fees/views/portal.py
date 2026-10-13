"""Student fees portal (FR-15, UC-12): own invoices and receipts, siblings' balances only.

A sibling row carries the first name, class and outstanding balance and nothing else: never
the last name, invoice lines, receipts or payments.
"""

from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.academics.services import current_session
from apps.fees import siblings
from apps.fees.invoicing import outstanding_for
from apps.fees.models import Invoice, Receipt
from apps.fees.views.pay import _student
from apps.students.models import Enrolment


def own_receipts(student):
    """Receipts this student may download: own payments, or allocations to own invoices."""
    return (
        Receipt.objects.filter(
            Q(payment__student=student) | Q(payment__allocations__invoice__student=student)
        )
        .select_related("payment")
        .distinct()
        .order_by("-pk")
    )


def _classes(student_ids: list[int]) -> dict[int, str]:
    session = current_session()
    if session is None or not student_ids:
        return {}
    enrolments = Enrolment.objects.filter(
        student_id__in=student_ids, session=session, status=Enrolment.Status.ACTIVE
    ).select_related("arm__level")
    return {e.student_id: e.arm.display_name for e in enrolments}


@never_cache
@login_required
@require_GET
def my_fees(request):
    student = _student(request)
    invoices = list(
        Invoice.objects.filter(student=student)
        .select_related("term__session")
        .prefetch_related("lines")
        .order_by("-term__session__start_date", "-term__number", "-pk")
    )
    sibs = siblings.siblings_of(student)
    classes = _classes([s.pk for s in sibs])
    sibling_rows = [
        {
            "first_name": s.first_name,
            "class_name": classes.get(s.pk, "—"),
            "balance": outstanding_for(s),
            "pk": s.pk,
        }
        for s in sibs
    ]
    owing_siblings = [row["pk"] for row in sibling_rows if row["balance"] > 0]
    pay_url = reverse("fees:pay")
    balance = outstanding_for(student)
    return render(
        request,
        "fees/my_fees.html",
        {
            "invoices": invoices,
            "balance": balance,
            "pay_url": pay_url if balance > 0 else "",
            "siblings": sibling_rows,
            "siblings_pay_url": (
                f"{pay_url}?{urlencode([('with', pk) for pk in owing_siblings])}"
                if owing_siblings
                else ""
            ),
            "receipts": own_receipts(student),
        },
    )
