"""Guardian notifications when a class is published (design §4.3, FR-08), sent through
``comms.notify`` since P6: one ``results_published`` message for the class, emailed to each
guardian address once (compared case-insensitively, so siblings share one email) and shown
in-app to each ranked student. Email only, no SMS (P6). Messages carry no scores, positions or
names.

The class is re-checked as still published, under the class's results lock, when the message
is created, and each publish event is messaged once (``dedupe_key`` holds the class and its
``published_at``): a retry or "Regenerate" of the same publish finds its message and stops,
while a re-publish after an unlock messages families again (P3 ruling, Ruling AG). Delivery
failures are logged by ``comms`` with the exception type only; the audit row holds counts only.
"""

import logging

from django.db import transaction

from apps.academics.models import ClassArm, Term
from apps.audit.services import record
from apps.comms.models import Message
from apps.comms.service import notify
from apps.core.locks import school_lock
from apps.results.models import ClassResult, ResultSummary
from apps.students.models import Student

logger = logging.getLogger(__name__)

SUBJECT = "Results are ready"
MESSAGE = "Results for {term} are ready. Log in to the student portal to view them."


def _escape(value: str) -> str:
    """Make a value safe to place in a ``notify`` body as literal text."""
    return value.replace("{", "{{").replace("}", "}}")


def ranked_students(arm: ClassArm, term: Term) -> list[Student]:
    """Every student ranked in this class (the message audience), in pk order."""
    ids = ResultSummary.objects.filter(arm=arm, term=term).values("student_id")
    return list(Student.objects.filter(pk__in=ids).order_by("pk"))


def publish_dedupe_key(klass: ClassResult) -> str:
    """One key per publish event: the class and its current ``published_at``."""
    stamp = klass.published_at.isoformat() if klass.published_at else "-"
    return f"results:{klass.pk}:{stamp}"


def notify_published(arm: ClassArm, term: Term) -> dict[str, int] | None:
    """Tell the class's families and students that results are out. Does nothing (None) unless
    the class is still published and this publish has not been messaged yet; both are checked
    under the ``results:{arm}:{term}`` lock, so an unlock cannot slip in before the message is
    created. Returns the counts it audited (email and in-app deliveries of the message)."""
    term = Term.objects.select_related("session").filter(pk=term.pk).first() or term
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        klass = ClassResult.objects.filter(arm=arm, term=term).first()
        if klass is None or klass.status != ClassResult.Status.PUBLISHED:
            logger.info(
                "Results notifications for class %s term %s skipped: not published.",
                arm.pk,
                term.pk,
            )
            return None
        key = publish_dedupe_key(klass)
        if Message.objects.filter(kind=Message.Kind.RESULTS_PUBLISHED, dedupe_key=key).exists():
            return None
        # ``{term}`` here is the published class's term, not the current one, so it is
        # rendered in before ``notify`` (escaped); the text is the P3 text, unchanged.
        message = notify(
            "results_published",
            students=ranked_students(arm, term),
            subject=SUBJECT,
            body=MESSAGE.format(term=_escape(str(term))),
            per_student=False,
            related=klass,
            audience_label=f"{arm.display_name} — {term}"[:120],
            dedupe_key=key,
        )
        counts = {
            "message_id": message.pk,
            "email_count": message.deliveries.filter(channel="email").count(),
            "inapp_count": message.deliveries.filter(channel="inapp").count(),
        }
        record("results.published_notified", obj=klass, after=dict(counts))
    return counts
