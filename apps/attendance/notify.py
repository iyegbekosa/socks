"""Absence emails to guardians (P5), sent through ``comms.notify`` since P6. One message per
student per date (email to the family and in-app to the student), never SMS.

The ``absence_emailed_at`` conditional update stays the send-once guard: it runs first, only
for records still ``absent``, and only the run that changes the row calls ``notify``. Absence
messages ignore quiet hours. Failures are logged with the exception type and record id only,
and audited as ``comms.notify_failed``.
"""

import logging
from collections.abc import Callable

from django.db import transaction
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.attendance.models import AttendanceRecord, AttendanceSettings
from apps.comms.models import SUBJECT_MAX, Message
from apps.comms.service import notify, record_notify_failure
from apps.core.logging import log_exception_redacted
from apps.students.models import Student

logger = logging.getLogger(__name__)

# The P5 text, unchanged; both placeholders are in the allowed set, filled per student.
MESSAGE = (
    "{student_first_name} was marked absent from school on {date}. "
    "Please contact the school if this is unexpected."
)


def _context_for(record: AttendanceRecord) -> Callable[[Student], dict[str, str]]:
    """The per-student placeholder values: the first name and the date of the absence."""
    when = date_format(record.date, "j F Y")
    return lambda student: {"student_first_name": student.first_name, "date": when}


def send_absence_emails(record_ids: list[int]) -> int:
    """Message the families of each record that is still absent and not yet messaged. Returns
    the number of records claimed. Does nothing when the school has the setting off."""
    if not AttendanceSettings.load().absence_email:
        return 0
    claimed = 0
    for record_id in record_ids:
        try:
            with transaction.atomic():
                # The conditional update is the send-once guard: only the run that changes the
                # row sends. A record corrected to present before the (delayed) run is skipped.
                changed = AttendanceRecord.objects.filter(
                    pk=record_id,
                    status=AttendanceRecord.Status.ABSENT,
                    absence_emailed_at__isnull=True,
                ).update(absence_emailed_at=timezone.now())
                if not changed:
                    continue
                record = AttendanceRecord.objects.select_related("student").get(pk=record_id)
                student = record.student
                notify(
                    "absence",
                    students=[student],
                    # Subjects are not rendered (Ruling AE): one call per student, literal name.
                    subject=f"Absence: {student.first_name}"[:SUBJECT_MAX],
                    body=MESSAGE,
                    per_student=True,
                    context_for=_context_for(record),
                    related=record,
                    audience_label="1 student",
                )
        except Exception:  # noqa: BLE001 - one bad record must not stop the rest
            # The claim rolled back with it, but nothing re-triggers this record: log it
            # (message redacted) and audit it so staff can see the absence went unreported.
            log_exception_redacted(logger, "Absence message for record %s failed", record_id)
            record_notify_failure(Message.Kind.ABSENCE.value, record_id)
            continue
        claimed += 1
    return claimed
