"""Debtors list (UC-14): who owes what as of a term, in a fixed number of queries."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import F, Max, Sum

from apps.academics.models import ClassArm, ClassLevel, Term
from apps.fees.invoicing import _earlier_terms_q
from apps.fees.models import Allocation, Invoice, Payment
from apps.students.models import Enrolment, Student


@dataclass(frozen=True)
class DebtorRow:
    student: Student
    arm: ClassArm | None
    total_kobo: int
    paid_kobo: int
    balance_kobo: int
    last_payment_at: datetime | None

    @property
    def withdrawn(self) -> bool:
        return self.student.status != Student.Status.ACTIVE


def debtors(
    term: Term,
    *,
    level: ClassLevel | None = None,
    arm: ClassArm | None = None,
    min_balance_kobo: int = 1,
) -> list[DebtorRow]:
    """Students owing at least ``min_balance_kobo`` on open invoices up to and including
    ``term``, largest balance first, then by admission number.

    Carried invoices are left out: their balance is counted once, as the brought-forward line
    of the later invoice. Withdrawn students are included (see ``DebtorRow.withdrawn``).
    ``level``/``arm`` filter on the student's enrolment in ``term.session``.
    """
    minimum = max(1, min_balance_kobo)
    invoices = (
        Invoice.objects.filter(_earlier_terms_q(term, inclusive=True))
        .exclude(status=Invoice.CARRIED)
        .filter(total_kobo__gt=F("paid_kobo"))
    )
    if level is not None or arm is not None:
        enrolled = Enrolment.objects.filter(session_id=term.session_id)
        if level is not None:
            enrolled = enrolled.filter(arm__level=level)
        if arm is not None:
            enrolled = enrolled.filter(arm=arm)
        invoices = invoices.filter(student_id__in=enrolled.values("student_id"))
    totals = list(
        invoices.order_by()
        .values("student_id")
        .annotate(
            total=Sum("total_kobo"),
            paid=Sum("paid_kobo"),
            balance=Sum(F("total_kobo") - F("paid_kobo")),
        )
        .filter(balance__gte=minimum)
    )
    if not totals:
        return []
    ids = [t["student_id"] for t in totals]
    students = Student.objects.select_related("user").in_bulk(ids)
    arms = {
        e.student_id: e.arm
        for e in Enrolment.objects.filter(session_id=term.session_id, student_id__in=ids)
        .select_related("arm__level")
        .order_by()
    }
    # The student's own payments, plus payments made for them (a sibling paying their invoice).
    last_paid: dict[int, datetime] = {}
    paid_by = (
        Payment.objects.filter(student_id__in=ids)
        .order_by()
        .values("student_id")
        .annotate(last=Max("paid_at"))
    )
    paid_for = (
        Allocation.objects.filter(invoice__student_id__in=ids)
        .order_by()
        .values("invoice__student_id")
        .annotate(last=Max("payment__paid_at"))
    )
    for sid, last in [(p["student_id"], p["last"]) for p in paid_by] + [
        (p["invoice__student_id"], p["last"]) for p in paid_for
    ]:
        if sid not in last_paid or last > last_paid[sid]:
            last_paid[sid] = last

    rows = [
        DebtorRow(
            student=students[t["student_id"]],
            arm=arms.get(t["student_id"]),
            total_kobo=int(t["total"]),
            paid_kobo=int(t["paid"]),
            balance_kobo=int(t["balance"]),
            last_payment_at=last_paid.get(t["student_id"]),
        )
        for t in totals
    ]
    rows.sort(key=lambda r: (-r.balance_kobo, r.student.admission_number or "", r.student.pk))
    return rows
