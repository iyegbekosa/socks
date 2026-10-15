"""The ``notify`` service (P6): every message to families goes through here.

``notify`` renders the body, de-duplicates recipients and writes the ``Message`` and its
``Delivery`` rows in one transaction. Email due now is enqueued on commit in batches of up to
100; bulk email composed in quiet hours waits for the 5-minute held-send job. In-app deliveries
are sent as soon as they are created.

Logs carry ids and exception types only; the audit holds counts only.
"""

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, time, timedelta
from email.utils import formataddr, parseaddr
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.academics.services import current_term
from apps.accounts.services import current_school, current_school_name
from apps.audit.services import record
from apps.comms.models import SUBJECT_MAX, CommsSettings, Delivery, Message
from apps.comms.render import check_template, render
from apps.core.logging import log_exception_redacted
from apps.students.models import Student

logger = logging.getLogger(__name__)

LAGOS = ZoneInfo("Africa/Lagos")
BATCH_SIZE = 100
# Comfortably above BATCH_SIZE x EMAIL_TIMEOUT (100 x 10 s), so a slow live batch is not swept.
STALE_CLAIM = timedelta(minutes=30)
BULK_KINDS = frozenset({"announcement", "fee_reminder", "results_published"})
PER_STUDENT_KINDS = frozenset({"fee_reminder", "receipt", "absence"})

EMAIL = Delivery.Channel.EMAIL
INAPP = Delivery.Channel.INAPP


def clock() -> datetime:
    """The time ``notify`` uses when it is not given ``now``. One seam, so the test suite can
    pin it to daytime and the quiet-hours rule never depends on when the suite runs."""
    return timezone.now()


def quiet_send_after(now: datetime, start: time, end: time) -> datetime | None:
    """The aware UTC time bulk email composed at ``now`` may go out, or None to send now.
    Quiet hours are ``[start, end)`` in Africa/Lagos, wrapping past midnight when start > end."""
    local = now.astimezone(LAGOS)
    t = local.time()
    quiet = (t >= start or t < end) if start > end else (start <= t < end)
    if not quiet:
        return None
    day = local.date() if t < end else local.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=LAGOS).astimezone(ZoneInfo("UTC"))


def _family_emails(student: Student) -> list[str]:
    out: list[str] = []
    for value in (student.guardian_email, student.guardian2_email):
        text = (value or "").strip().lower()
        if text and text not in out:
            out.append(text)
    return out


def _base_context(now: datetime) -> dict[str, str]:
    term = current_term()
    return {
        "school_name": current_school_name(),
        "term": str(term) if term else "",
        "date": date_format(now.astimezone(LAGOS).date(), "j F Y"),
    }


def sample_context() -> dict[str, str]:
    """The shared placeholder values a message composed now would get (for previews)."""
    return _base_context(clock())


def recipient_counts(students: Iterable[Student]) -> dict[str, int]:
    """The deliveries ``notify`` would create for a shared (not per-student) body, counted
    without writing anything: family emails de-duplicated across the whole message, and one
    in-app delivery per student user."""
    emails: set[str] = set()
    users: set[int | None] = set()
    seen: set[int] = set()
    for student in students:
        if student.pk in seen:
            continue
        seen.add(student.pk)
        emails.update(_family_emails(student))
        users.add(student.user_id)
    return {"email_count": len(emails), "inapp_count": len(users)}


def _chunks(ids: list[int]) -> Iterable[list[int]]:
    for i in range(0, len(ids), BATCH_SIZE):
        yield ids[i : i + BATCH_SIZE]


def enqueue_batches(ids: list[int]) -> None:
    """Queue ``send_email_batch_task`` for ``ids`` in chunks of 100. A broker failure is
    logged by type: the rows stay queued and the held-send job picks them up."""
    from apps.comms.tasks import send_email_batch_task

    for chunk in _chunks(ids):
        try:
            send_email_batch_task.apply_async(args=[chunk], queue="notifications")
        except Exception as exc:  # noqa: BLE001 - the 5-minute job retries queued rows
            logger.warning(
                "Could not queue an email batch of %s deliveries (%s).",
                len(chunk),
                type(exc).__name__,
            )


def notify(
    kind: str,
    *,
    students: list[Student],
    subject: str,
    body: str,
    per_student: bool,
    context_for: Callable[[Student], dict] | None = None,
    channels: Iterable[str] = ("email", "inapp"),
    by: Any = None,
    related: Any = None,
    audience_label: str = "",
    now: datetime | None = None,
    dedupe_key: str = "",
) -> Message:
    """Send ``body`` to the families (email) and student users (in-app) of ``students``.

    Family email is de-duplicated case-insensitively across the whole message, so a guardian
    of two siblings gets one email; when ``per_student`` the body differs per student and
    de-duplication is per (email, student) instead. ``context_for(student)`` adds per-student
    placeholder values. ``dedupe_key`` marks an automatic notice that must go once: a second
    message of the same kind and key raises ``IntegrityError`` (callers check first under their
    own lock). Raises ``ValidationError`` for a bad subject or placeholder."""
    if kind not in Message.Kind.values:
        raise ValueError(f"Unknown message kind {kind!r}")
    if per_student and kind not in PER_STUDENT_KINDS:
        raise ValueError(f"{kind} messages are not per-student")
    subject = (subject or "").strip()
    if not subject:
        raise ValidationError("Give the message a subject.")
    if len(subject) > SUBJECT_MAX:
        raise ValidationError(f"Keep the subject to {SUBJECT_MAX} characters or fewer.")
    check_template(body, per_student=per_student)
    chosen = set(channels)
    if not chosen or not chosen <= {EMAIL, INAPP}:
        raise ValueError(f"Unknown channels {sorted(chosen)!r}")
    now = now or clock()

    with transaction.atomic():
        base = _base_context(now)
        send_after = None
        if kind in BULK_KINDS and EMAIL in chosen:
            conf = CommsSettings.load()
            send_after = quiet_send_after(now, conf.email_quiet_start, conf.email_quiet_end)
        message = Message.objects.create(
            kind=kind,
            subject=subject,
            body=body,
            audience_label=audience_label,
            created_by=by if getattr(by, "pk", None) else None,
            created_at=now,
            related_type=(
                f"{related._meta.app_label}.{related._meta.model_name}" if related else ""
            ),
            related_id=related.pk if related else None,
            dedupe_key=dedupe_key,
        )
        rows: list[Delivery] = []
        seen_students: set[int] = set()
        seen_emails: set[tuple[str, int | None]] = set()
        seen_users: set[int] = set()
        shared_text = None if per_student else render(body, base)
        for student in students:
            if student.pk in seen_students:
                continue
            seen_students.add(student.pk)
            if shared_text is None:
                extra = context_for(student) if context_for else {}
                text = render(body, {**base, **{k: str(v) for k, v in extra.items()}})
            else:
                text = shared_text
            if EMAIL in chosen:
                for address in _family_emails(student):
                    key = (address, student.pk if per_student else None)
                    if key in seen_emails:
                        continue
                    seen_emails.add(key)
                    rows.append(
                        Delivery(
                            message=message,
                            channel=EMAIL,
                            student=student,
                            email=address,
                            body=text,
                            status=Delivery.Status.QUEUED,
                            queued_at=now,
                            send_after=send_after,
                        )
                    )
            if INAPP in chosen and student.user_id not in seen_users:
                seen_users.add(student.user_id)
                rows.append(
                    Delivery(
                        message=message,
                        channel=INAPP,
                        student=student,
                        user_id=student.user_id,
                        body=text,
                        status=Delivery.Status.SENT,
                        queued_at=now,
                        sent_at=now,
                    )
                )
        created = Delivery.objects.bulk_create(rows)
        email_rows = [d for d in created if d.channel == EMAIL]
        counts = {"email_count": len(email_rows), "inapp_count": len(created) - len(email_rows)}
        record(
            "comms.message_sent",
            actor=by,
            obj=message,
            after={"message_id": message.pk, "kind": kind, **counts},
        )
        due = [d.pk for d in email_rows if d.send_after is None]
        if due:
            transaction.on_commit(lambda: enqueue_batches(due))
    return message


def record_notify_failure(kind: str, related_id: int | None) -> None:
    """Leave a visible trace of an automatic message that could not be created:
    ``comms.notify_failed`` with the kind and the related row's id (ids only). For senders
    that never raise; runs in its own savepoint and never raises itself."""
    try:
        with transaction.atomic():
            record("comms.notify_failed", after={"kind": kind, "related_id": related_id})
    except Exception:  # noqa: BLE001 - the sender has already logged the failure
        log_exception_redacted(logger, "Auditing a failed %s message failed", kind)


def _due(now: datetime) -> Q:
    return Q(send_after__isnull=True) | Q(send_after__lte=now)


def claim(ids: list[int], now: datetime) -> list[int]:
    """Move the still-queued, due email deliveries among ``ids`` to ``sending`` and return
    the ones this caller claimed. Rows locked by a concurrent claim are skipped."""
    with transaction.atomic():
        pks = list(
            Delivery.objects.select_for_update(skip_locked=True)
            .filter(pk__in=ids, channel=EMAIL, status=Delivery.Status.QUEUED)
            .filter(_due(now))
            .values_list("pk", flat=True)
        )
        Delivery.objects.filter(pk__in=pks, status=Delivery.Status.QUEUED).update(
            status=Delivery.Status.SENDING, claimed_at=now
        )
    return pks


def _mine(pk: int, claimed_at: datetime) -> Q:
    """The row ``pk`` while it is still this batch's claim: ``claimed_at`` is the ownership
    token, so a row swept as stale and re-claimed by another batch is left alone."""
    return Q(pk=pk, status=Delivery.Status.SENDING, claimed_at=claimed_at)


def _finish(pk: int, claimed_at: datetime, status: str, error_type: str = "") -> bool:
    return bool(
        Delivery.objects.filter(_mine(pk, claimed_at)).update(
            status=status,
            error_type=error_type[:80],
            sent_at=timezone.now() if status == Delivery.Status.SENT else None,
        )
    )


def sender() -> tuple[str, list[str]]:
    """The From header and Reply-To list for this school's email: the school's name on the
    platform address (``DEFAULT_FROM_EMAIL`` may carry its own display name, which is
    replaced), replying to the school's contact email when it has one."""
    school = current_school()
    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1] or settings.DEFAULT_FROM_EMAIL
    reply_to = [school.contact_email] if school.contact_email else []
    return formataddr((str(school.name), address)), reply_to


def send_batch(ids: list[int]) -> dict[str, int]:
    """Send the claimable email deliveries among ``ids`` over one connection, marking each
    ``sent`` or ``failed``. Failures are logged by type and id; the batch continues.

    If the connection cannot be opened nothing was handed to the provider, so the claimed rows
    go back to ``queued`` (no error recorded) and the 5-minute job retries them. Each row is
    sent and finished only while its ``claimed_at`` is still this batch's claim time."""
    claimed_at = timezone.now()
    pks = claim(list(ids)[:BATCH_SIZE], claimed_at)
    counts = {"sent": 0, "failed": 0, "requeued": 0}
    if not pks:
        return counts
    deliveries = list(Delivery.objects.filter(pk__in=pks).select_related("message").order_by("pk"))
    from_email, reply_to = sender()
    connection = get_connection()
    try:
        connection.open()
    except Exception as exc:  # noqa: BLE001 - nothing was sent; the held-send job retries
        logger.warning(
            "Email connection failed (%s); %s deliveries returned to the queue.",
            type(exc).__name__,
            len(pks),
        )
        counts["requeued"] = Delivery.objects.filter(
            pk__in=pks, status=Delivery.Status.SENDING, claimed_at=claimed_at
        ).update(status=Delivery.Status.QUEUED, claimed_at=None, error_type="")
        return counts
    try:
        for d in deliveries:
            if not Delivery.objects.filter(_mine(d.pk, claimed_at)).exists():
                continue  # swept as stale and re-claimed elsewhere: not ours to send
            email = EmailMessage(
                subject=d.message.subject,
                body=d.body,
                from_email=from_email,
                to=[d.email],
                reply_to=reply_to,
                connection=connection,
            )
            try:
                error = "" if connection.send_messages([email]) else "NotSent"
            except Exception as exc:  # noqa: BLE001 - one bad address must not stop the rest
                error = type(exc).__name__
            if error:
                logger.warning("Email delivery %s failed (%s).", d.pk, error)
                if _finish(d.pk, claimed_at, Delivery.Status.FAILED, error):
                    counts["failed"] += 1
            elif _finish(d.pk, claimed_at, Delivery.Status.SENT):
                counts["sent"] += 1
    finally:
        try:
            connection.close()
        except Exception as exc:  # noqa: BLE001 - statuses are already recorded
            logger.warning("Closing the email connection failed (%s).", type(exc).__name__)
    return counts


def release_and_enqueue_held() -> int:
    """The 5-minute job for this school: return ``sending`` rows claimed over 30 minutes ago
    to ``queued`` (a crashed worker must not strand them), then enqueue every due queued email
    in batches of 100. Returns the number of deliveries enqueued."""
    now = timezone.now()
    Delivery.objects.filter(channel=EMAIL, status=Delivery.Status.SENDING).filter(
        Q(claimed_at__lt=now - STALE_CLAIM) | Q(claimed_at__isnull=True)
    ).update(status=Delivery.Status.QUEUED, claimed_at=None)
    ids = list(
        Delivery.objects.filter(channel=EMAIL, status=Delivery.Status.QUEUED)
        .filter(_due(now))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    enqueue_batches(ids)
    return len(ids)


def requeue_failed(message: Message, *, by: Any, request: Any = None) -> int:
    """ "Retry failed": return the message's ``failed`` email deliveries to ``queued`` (clearing
    ``error_type`` and the claim) and enqueue them in batches on commit. Audited as
    ``comms.retry_failed`` with ``{message_id, count}`` when anything was re-queued. A bulk
    message retried in quiet hours is held until they end, like a newly composed one."""
    with transaction.atomic():
        ids = list(
            Delivery.objects.select_for_update()
            .filter(message=message, channel=EMAIL, status=Delivery.Status.FAILED)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not ids:
            return 0
        held = None
        if message.kind in BULK_KINDS:
            conf = CommsSettings.load()
            held = quiet_send_after(clock(), conf.email_quiet_start, conf.email_quiet_end)
        extra = {"send_after": held} if held else {}
        Delivery.objects.filter(pk__in=ids, status=Delivery.Status.FAILED).update(
            status=Delivery.Status.QUEUED, error_type="", claimed_at=None, sent_at=None, **extra
        )
        record(
            "comms.retry_failed",
            request=request,
            actor=by,
            obj=message,
            after={"message_id": message.pk, "count": len(ids)},
        )
        if not held:
            transaction.on_commit(lambda: enqueue_batches(ids))
    return len(ids)
