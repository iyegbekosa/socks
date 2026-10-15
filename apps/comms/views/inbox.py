from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.comms.inbox import inbox_for, mark_all_read, mark_read, unread_count
from apps.core.parsing import parse_id

PAGE_SIZE = 20


def _student_only(request):
    if request.user.kind != "student":
        raise PermissionDenied


@never_cache
@login_required
def inbox(request):
    _student_only(request)
    page = Paginator(inbox_for(request.user), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "comms/inbox.html",
        {"page": page, "unread": unread_count(request.user)},
    )


@never_cache
@login_required
def inbox_item(request, pk):
    _student_only(request)
    delivery_id = parse_id(pk)
    if delivery_id is None:
        raise Http404
    delivery = inbox_for(request.user).filter(pk=delivery_id).first()
    if delivery is None:
        raise Http404
    mark_read(delivery)
    return render(request, "comms/inbox_item.html", {"delivery": delivery})


@require_POST
@login_required
def inbox_read_all(request):
    _student_only(request)
    mark_all_read(request.user)
    return redirect("comms:inbox")
