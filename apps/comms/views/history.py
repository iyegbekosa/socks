"""Message history (``comms:history``), one message's delivery status (``comms:message``) and
"Retry failed" (``comms:retry_failed``). Guardian emails are always shown masked."""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.academics.services import current_session
from apps.accounts.roles import BURSAR, SCHOOL_ADMIN, require_roles
from apps.comms import service
from apps.comms.models import Delivery, Message
from apps.comms.scope import (
    HISTORY_ROLES,
    RETRY_ROLES,
    audiences_for,
    retryable_messages,
    visible_messages,
    with_failures,
)
from apps.comms.service import requeue_failed
from apps.core.parsing import parse_id
from apps.students.models import Enrolment

PAGE_SIZE = 25
DELIVERY_PAGE_SIZE = 100
S = Delivery.Status


def mask_email(email: str) -> str:
    """``a***@domain``: the first character, ``***``, then the domain."""
    if not email:
        return ""
    local, at, domain = email.partition("@")
    if not at or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def _with_counts(qs):
    return qs.annotate(
        sent_count=Count("deliveries", filter=Q(deliveries__status=S.SENT)),
        queued_count=Count("deliveries", filter=Q(deliveries__status__in=[S.QUEUED, S.SENDING])),
        failed_count=Count("deliveries", filter=Q(deliveries__status=S.FAILED)),
        read_count=Count("deliveries", filter=Q(deliveries__read_at__isnull=False)),
    )


@never_cache
@require_roles(*HISTORY_ROLES)
def history(request):
    qs = visible_messages(request.user).select_related("created_by")
    kind = request.GET.get("kind", "")
    if kind in Message.Kind.values:
        qs = qs.filter(kind=kind)
    else:
        kind = ""
    failed = request.GET.get("failed") == "1"
    if failed:
        qs = with_failures(qs)
    ordered = _with_counts(qs).order_by("-created_at", "-pk")  # aggregates drop Meta.ordering
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "comms/history.html",
        {
            "page": page,
            "kind": kind,
            "failed": failed,
            "filters": urlencode(
                {k: v for k, v in (("kind", kind), ("failed", "1" if failed else "")) if v}
            ),
            "kinds": Message.Kind.choices,
            "can_compose": bool(audiences_for(request.user)),
            "can_manage_reminders": request.user.has_role(BURSAR, SCHOOL_ADMIN),
        },
    )


def _message_or_404(qs, pk):
    message_id = parse_id(pk)
    message = qs.filter(pk=message_id).first() if message_id is not None else None
    if message is None:
        raise Http404
    return message


def _classes(student_ids: list[int]) -> dict[int, str]:
    session = current_session()
    if session is None or not student_ids:
        return {}
    enrolments = Enrolment.objects.filter(
        session=session, status=Enrolment.Status.ACTIVE, student_id__in=student_ids
    ).select_related("arm__level")
    return {e.student_id: e.arm.display_name for e in enrolments}


@never_cache
@require_roles(*HISTORY_ROLES)
def message_detail(request, pk):
    message = _message_or_404(visible_messages(request.user).select_related("created_by"), pk)
    deliveries = Delivery.objects.filter(message=message).select_related("student")
    page = Paginator(deliveries.order_by("channel", "pk"), DELIVERY_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    classes = _classes([d.student_id for d in page.object_list if d.channel == "inapp"])
    rows = [
        {
            "recipient": (
                mask_email(d.email) if d.channel == Delivery.Channel.EMAIL else d.student.first_name
            ),
            "klass": classes.get(d.student_id, "") if d.channel == Delivery.Channel.INAPP else "",
            "channel": d.get_channel_display(),
            "status": d.get_status_display(),
            "error_type": d.error_type,
            "sent_at": d.sent_at,
            "read": d.read_at is not None,
        }
        for d in page.object_list
    ]
    totals = _with_counts(Message.objects.filter(pk=message.pk)).values(
        "sent_count", "queued_count", "failed_count", "read_count"
    )[0]
    can_retry = (
        request.user.has_role(*RETRY_ROLES)
        and retryable_messages(request.user).filter(pk=message.pk).exists()
    )
    return render(
        request,
        "comms/message.html",
        {
            "message": message,
            "page": page,
            "rows": rows,
            "totals": totals,
            "can_retry": can_retry,
        },
    )


@require_POST
@require_roles(*RETRY_ROLES)
def retry_failed(request, pk):
    message = _message_or_404(retryable_messages(request.user), pk)
    count = requeue_failed(message, by=request.user, request=request)
    held = Delivery.objects.filter(
        message=message, status=S.QUEUED, send_after__gt=service.clock()
    ).exists()
    if count and held:
        messages.success(
            request, f"{count} failed email(s) queued again. They go out when quiet hours end."
        )
    elif count:
        messages.success(request, f"{count} failed email(s) queued again.")
    else:
        messages.info(request, "No failed emails to retry.")
    return redirect("comms:message", message.pk)
