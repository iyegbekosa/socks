"""Absence emails to guardians (P5). One email per student per date, never SMS.

Send failures are logged with the exception type and record id only and never raised.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.attendance.models import AttendanceRecord, AttendanceSettings

logger = logging.getLogger(__name__)

MESSAGE = (
    "{name} was marked absent from school on {date}. "
    "Please contact the school if this is unexpected."
)


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


def send_absence_emails(record_ids: list[int]) -> int:
    """Email the guardians of each record that is still absent and not yet emailed. Returns
    the number of records claimed. Does nothing when the school has the setting off."""
    if not AttendanceSettings.load().absence_email:
        return 0
    claimed = 0
    for record_id in record_ids:
        # The conditional update is the send-once guard: only the run that changes the row sends.
        changed = AttendanceRecord.objects.filter(
            pk=record_id,
            status=AttendanceRecord.Status.ABSENT,
            absence_emailed_at__isnull=True,
        ).update(absence_emailed_at=timezone.now())
        if not changed:
            continue
        claimed += 1
        record = AttendanceRecord.objects.select_related("student").get(pk=record_id)
        student = record.student
        recipients = _unique([student.guardian_email, student.guardian2_email])
        if not recipients:
            continue
        body = MESSAGE.format(name=student.first_name, date=date_format(record.date, "j F Y"))
        for address in recipients:
            try:
                send_mail(
                    subject=f"Absence: {student.first_name}",
                    message=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[address],
                    fail_silently=False,
                )
            except Exception as exc:  # noqa: BLE001 - one bad address must not stop the rest
                logger.warning(
                    "Absence email for record %s failed (%s).", record_id, type(exc).__name__
                )
    return claimed
