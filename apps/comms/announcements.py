"""Announcements composed by staff (HTML ``comms:compose`` and API ``api:messages``): the
limits, the preview and the send, shared so both front doors behave the same."""

from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Count, Q

from apps.comms.audience import Audience, label, resolve
from apps.comms.models import SUBJECT_MAX, Delivery, Message
from apps.comms.render import render
from apps.comms.service import notify, recipient_counts, sample_context
from apps.students.models import Student

BODY_MAX = 2000
NO_STUDENTS = "No students match this audience."

__all__ = ["BODY_MAX", "NO_STUDENTS", "SUBJECT_MAX", "delivery_counts", "preview", "send"]


def students_for(audience: Audience) -> list[Student]:
    """The audience's active students; raises ``ValidationError`` when there are none."""
    students = resolve(audience)
    if not students:
        raise ValidationError(NO_STUDENTS)
    return students


def preview(students: list[Student], body: str) -> dict[str, Any]:
    """Recipient counts and the rendered body, with nothing written."""
    return {**recipient_counts(students), "body": render(body, sample_context())}


def send(
    audience: Audience,
    students: list[Student],
    *,
    subject: str,
    body: str,
    by: Any,
    dedupe_key: str = "",
) -> Message:
    """Send one announcement (shared body, family emails de-duplicated) through ``notify``."""
    return notify(
        "announcement",
        students=students,
        subject=subject,
        body=body,
        per_student=False,
        by=by,
        audience_label=label(audience),
        dedupe_key=dedupe_key,
    )


def delivery_counts(message: Message) -> dict[str, int]:
    """``{email_count, inapp_count}`` for ``message``, in one query."""
    return Delivery.objects.filter(message=message).aggregate(
        email_count=Count("pk", filter=Q(channel=Delivery.Channel.EMAIL)),
        inapp_count=Count("pk", filter=Q(channel=Delivery.Channel.INAPP)),
    )
