"""A student's in-app inbox: the queries shared by the HTML pages, the API and the portal."""

from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.comms.models import Delivery


def inbox_for(user: Any) -> QuerySet[Delivery]:
    """The user's own in-app deliveries, newest first."""
    return (
        Delivery.objects.filter(user=user, channel=Delivery.Channel.INAPP)
        .select_related("message")
        .order_by("-message__created_at", "-pk")
    )


def unread_count(user: Any) -> int:
    """How many in-app deliveries the user has not opened (one query)."""
    return Delivery.objects.filter(
        user=user, channel=Delivery.Channel.INAPP, read_at__isnull=True
    ).count()


def mark_read(delivery: Delivery) -> None:
    """Set ``read_at`` once; later opens leave the first time in place."""
    if delivery.read_at is None:
        Delivery.objects.filter(pk=delivery.pk, read_at__isnull=True).update(read_at=timezone.now())


def mark_all_read(user: Any) -> int:
    return Delivery.objects.filter(
        user=user, channel=Delivery.Channel.INAPP, read_at__isnull=True
    ).update(read_at=timezone.now())
