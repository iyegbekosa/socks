"""Guardian notifications when a class is published (design §4.3, FR-08).

Each guardian email address and phone number in the class is contacted once (compared
case-insensitively, so siblings share one message). Messages carry no scores, positions or
names. Send failures are logged with the exception type only and never raised; the audit row
holds counts only.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

from apps.academics.models import ClassArm, Term
from apps.audit.services import record
from apps.core.sms import send_sms, sms_configured
from apps.results.models import ClassResult, ResultSummary
from apps.students.models import Student

logger = logging.getLogger(__name__)

MESSAGE = "Results for {term} are ready. Log in to the student portal to view them."


def _unique(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = (value or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def guardian_contacts(arm: ClassArm, term: Term) -> tuple[list[str], list[str]]:
    """(emails, phones) of the guardians of every student ranked in this class, deduplicated."""
    ids = ResultSummary.objects.filter(arm=arm, term=term).values("student_id")
    rows = list(
        Student.objects.filter(pk__in=ids)
        .order_by("pk")
        .values_list("guardian_email", "guardian2_email", "guardian_phone", "guardian2_phone")
    )
    emails = _unique([e for row in rows for e in row[:2]])
    phones = _unique([p for row in rows for p in row[2:]])
    return emails, phones


def _still_published(arm: ClassArm, term: Term) -> bool:
    return ClassResult.objects.filter(
        arm=arm, term=term, status=ClassResult.Status.PUBLISHED
    ).exists()


def notify_published(arm: ClassArm, term: Term) -> dict[str, int] | None:
    """Tell the class's guardians that results are out. Does nothing (None) unless the class is
    still published, and stops sending if it is unlocked part-way (re-checked before each
    recipient). Returns the counts it audited. SMS are counted when queued (``send_sms``
    enqueues delivery), hence ``sms_queued``."""
    klass = ClassResult.objects.filter(arm=arm, term=term).first()
    if klass is None or klass.status != ClassResult.Status.PUBLISHED:
        return None
    term = Term.objects.select_related("session").get(pk=term.pk)
    body = MESSAGE.format(term=term)
    emails, phones = guardian_contacts(arm, term)
    counts = {"emails_sent": 0, "sms_queued": 0, "failures": 0}
    stopped = False
    for address in emails:
        if not _still_published(arm, term):
            stopped = True
            break
        try:
            send_mail(
                subject="Results are ready",
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[address],
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001 - one bad address must not stop the rest
            counts["failures"] += 1
            logger.warning(
                "Results email for class %s term %s failed (%s).",
                arm.pk,
                term.pk,
                type(exc).__name__,
            )
        else:
            counts["emails_sent"] += 1
    if sms_configured() and not stopped:
        for phone in phones:
            if not _still_published(arm, term):
                stopped = True
                break
            try:
                send_sms(phone, body)
            except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
                counts["failures"] += 1
                logger.warning(
                    "Results SMS for class %s term %s failed (%s).",
                    arm.pk,
                    term.pk,
                    type(exc).__name__,
                )
            else:
                counts["sms_queued"] += 1
    if stopped:
        logger.info(
            "Results notifications for class %s term %s stopped: the class is no longer published.",
            arm.pk,
            term.pk,
        )
    record("results.published_notified", obj=klass, after=dict(counts))
    return counts
