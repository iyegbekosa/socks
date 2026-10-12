import logging

from apps.accounts.services import (
    current_school_name,
    find_user_by_identifier,
    issue_reset_code,
    reset_email_for,
    reset_phone_for,
    send_reset_link_email,
)
from apps.audit.services import record
from apps.core.sms import send_sms, sms_configured
from apps.core.tasks import TenantTask
from config.celery import app

logger = logging.getLogger(__name__)


@app.task(base=TenantTask, queue="notifications")
def send_password_reset(identifier: str, base_url: str, ip: str | None, request_id: str) -> None:
    """Look the account up and send the reset link and/or code.

    Runs in the worker so the reset request takes the same time for known and unknown
    identifiers. The link is a credential and is never logged.
    """
    user = find_user_by_identifier(identifier)
    if user is None:
        return
    email = reset_email_for(user)
    if email:
        try:
            send_reset_link_email(user, email, base_url)
        except Exception:  # noqa: BLE001 - never log the link or the address
            logger.warning("Reset link email could not be sent")
        else:
            # No address in the audit row: only that a link went out.
            record(
                "auth.password_reset_link_sent",
                obj=user,
                ip=ip or None,
                request_id=request_id or "",
            )
    if sms_configured():
        phone = reset_phone_for(user)
        code = issue_reset_code(user) if phone else None
        if code:
            send_sms(
                phone,  # type: ignore[arg-type]
                f"{current_school_name()}: your password reset code is {code}. "
                "It expires in 10 minutes.",
            )
