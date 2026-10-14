"""Things that happen after a register save commits."""

import logging

from apps.attendance.models import AttendanceSettings

logger = logging.getLogger(__name__)


def on_register_saved(record_ids: list[int]) -> None:
    """Called on commit with the ids of records that became ``absent`` in a save: queue the
    guardian emails when the school has them on. A broker failure is logged by type and
    swallowed; the register is already saved."""
    from apps.attendance.tasks import send_absence_emails_task

    if not record_ids or not AttendanceSettings.load().absence_email:
        return
    try:
        send_absence_emails_task.apply_async(args=[record_ids], queue="notifications")
    except Exception as exc:  # noqa: BLE001 - a broker outage must not undo the save
        logger.warning("Could not queue absence emails (%s).", type(exc).__name__)
