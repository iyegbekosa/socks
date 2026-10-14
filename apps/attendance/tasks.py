from apps.attendance.notify import send_absence_emails
from apps.core.tasks import TenantTask
from config.celery import app


@app.task(base=TenantTask, queue="notifications")
def send_absence_emails_task(record_ids: list[int]) -> None:
    """Email guardians about newly absent students; see ``notify.send_absence_emails``. Args are
    redacted from Celery logs (``SECRET_ARG_TASKS``)."""
    send_absence_emails(record_ids)
