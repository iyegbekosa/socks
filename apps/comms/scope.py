"""Who may compose announcements, see message history and retry failed emails (§6.2, §7.1).

- school_admin and principal: every audience, every message, "Retry failed" on any of them.
- bursar: the ``debtors`` audience only; sees fee reminders, receipts and the announcements
  they sent, and may retry those.
- proprietor: reads history and message pages only.
- everyone else: 403.
"""

from datetime import timedelta
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.accounts.roles import BURSAR, PRINCIPAL, PROPRIETOR, SCHOOL_ADMIN
from apps.comms.audience import TYPES
from apps.comms.models import Delivery, Message

COMPOSE_ROLES = (SCHOOL_ADMIN, PRINCIPAL, BURSAR)
HISTORY_ROLES = (SCHOOL_ADMIN, PRINCIPAL, BURSAR, PROPRIETOR)
RETRY_ROLES = (SCHOOL_ADMIN, PRINCIPAL, BURSAR)
FULL_ROLES = (SCHOOL_ADMIN, PRINCIPAL)
BURSAR_AUDIENCES = ("debtors",)
BURSAR_KINDS = (Message.Kind.FEE_REMINDER, Message.Kind.RECEIPT)


def _staff_has(user: Any, *roles: str) -> bool:
    return bool(user.is_authenticated and user.kind == "staff" and user.has_role(*roles))


def audiences_for(user: Any) -> tuple[str, ...]:
    """The audience types ``user`` may send an announcement to (empty: may not compose)."""
    if _staff_has(user, *FULL_ROLES):
        return TYPES
    if _staff_has(user, BURSAR):
        return BURSAR_AUDIENCES
    return ()


def _bursar_q(user: Any) -> Q:
    return Q(kind__in=BURSAR_KINDS) | Q(kind=Message.Kind.ANNOUNCEMENT, created_by=user)


def visible_messages(user: Any) -> QuerySet[Message]:
    """The messages ``user`` may see in history and on the message page."""
    qs = Message.objects.all()
    if _staff_has(user, SCHOOL_ADMIN, PRINCIPAL, PROPRIETOR):
        return qs
    if _staff_has(user, BURSAR):
        return qs.filter(_bursar_q(user))
    return qs.none()


def retryable_messages(user: Any) -> QuerySet[Message]:
    """The messages whose failed emails ``user`` may re-queue."""
    qs = Message.objects.all()
    if _staff_has(user, *FULL_ROLES):
        return qs
    if _staff_has(user, BURSAR):
        return qs.filter(_bursar_q(user))
    return qs.none()


FAILED_WINDOW = timedelta(days=7)


def with_failures(qs: QuerySet[Message]) -> QuerySet[Message]:
    """The messages in ``qs`` with at least one failed delivery."""
    failed = Delivery.objects.filter(message=OuterRef("pk"), status=Delivery.Status.FAILED)
    return qs.filter(Exists(failed))


def recent_failed_count(user: Any) -> int:
    """How many messages ``user`` may see that were sent in the last 7 days and have a failed
    delivery (one query; 0 for users without history access)."""
    if not _staff_has(user, *HISTORY_ROLES):
        return 0
    since = timezone.now() - FAILED_WINDOW
    return with_failures(visible_messages(user).filter(created_at__gte=since)).count()
