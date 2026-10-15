"""Scheduled fee reminders (P6): on each reminder date of the current term, families of
students owing at least the school's minimum balance get one ``fee_reminder`` message.

The message's ``dedupe_key`` is ``fee_reminder:<date>``, so a school sends at most one a day
whether the Beat job runs twice or the bursar pressed "Send reminders now" first. The school
lock plus an existing-message check make a second run a clean no-op; the unique constraint is
the backstop.
"""

import logging
from datetime import date

from django.db import transaction

from apps.academics.models import Term
from apps.academics.services import current_term
from apps.comms import audience
from apps.comms.models import CommsSettings, FeeReminderDate, Message
from apps.comms.service import notify, term_text
from apps.core.locks import school_lock
from apps.core.money import naira_display
from apps.fees.debtors import debtors
from apps.students.models import Student

logger = logging.getLogger(__name__)

SUBJECT = "School fees reminder"
MAX_DATES = 6


def dedupe_key(today: date) -> str:
    return f"fee_reminder:{today.isoformat()}"


def send_due_reminders(today: date) -> Message | None:
    """The scheduled job for this school: send today's reminder when ``today`` is a reminder
    date of the current term. Returns the message, or None when nothing was sent."""
    term = current_term()
    if term is None or not FeeReminderDate.objects.filter(term=term, date=today).exists():
        return None
    return _send(term, today, by=None)


def send_now(today: date, *, by=None) -> Message | None:
    """ "Send reminders now": the same reminder, on any day, counting as today's."""
    term = current_term()
    if term is None:
        return None
    return _send(term, today, by=by)


def _send(term: Term, today: date, *, by) -> Message | None:
    key = dedupe_key(today)
    with transaction.atomic():
        school_lock("comms:reminders")
        if Message.objects.filter(kind=Message.Kind.FEE_REMINDER, dedupe_key=key).exists():
            return None
        conf = CommsSettings.load()
        rows = [
            r
            for r in debtors(term, min_balance_kobo=conf.reminder_min_balance_kobo)
            if r.student.status == Student.Status.ACTIVE
        ]
        if not rows:
            return None
        balances = {r.student.pk: r.balance_kobo for r in rows}
        label = audience.label(
            audience.Audience(
                type="debtors", term=term.pk, min_balance_kobo=conf.reminder_min_balance_kobo
            )
        )
        return notify(
            "fee_reminder",
            students=[r.student for r in rows],
            subject=SUBJECT,
            body=conf.reminder_template,
            per_student=True,
            context_for=lambda s: {
                "student_first_name": s.first_name,
                "term": term_text(term),
                "balance": naira_display(balances[s.pk]),
            },
            by=by,
            related=term,
            audience_label=label,
            dedupe_key=key,
        )
