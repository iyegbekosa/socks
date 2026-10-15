"""Things that happen after a register save commits."""

import logging
from typing import Any

from apps.attendance.models import AttendanceSettings

logger = logging.getLogger(__name__)


def on_register_saved(record_ids: list[int]) -> None:
    """Called on commit with the ids of records that became ``absent`` in a save: queue the
    guardian emails when the school has them on, ``absence_email_delay_minutes`` from now (the
    task emails only records still absent then, so a corrected mark sends nothing). A broker
    failure is logged by type and swallowed; the register is already saved."""
    from apps.attendance.tasks import send_absence_emails_task

    if not record_ids:
        return
    conf = AttendanceSettings.load()
    if not conf.absence_email:
        return
    options: dict[str, Any] = {"queue": "notifications"}
    if conf.absence_email_delay_minutes:
        options["countdown"] = conf.absence_email_delay_minutes * 60
    try:
        send_absence_emails_task.apply_async(args=[record_ids], **options)
    except Exception as exc:  # noqa: BLE001 - a broker outage must not undo the save
        logger.warning("Could not queue absence emails (%s).", type(exc).__name__)
