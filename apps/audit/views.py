from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from apps.accounts.roles import PROPRIETOR, SCHOOL_ADMIN, require_roles
from apps.audit.models import AuditEvent

PAGE_SIZE = 50
MAX_ACTION_LENGTH = 64


@never_cache
@require_roles(SCHOOL_ADMIN, PROPRIETOR)
def audit_log(request):
    action = request.GET.get("action", "").strip()[:MAX_ACTION_LENGTH]
    events = AuditEvent.objects.order_by("-at", "-pk")
    if action:
        events = events.filter(action__startswith=action)
    page_obj = Paginator(events, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "audit/log.html", {"page_obj": page_obj, "action": action})
