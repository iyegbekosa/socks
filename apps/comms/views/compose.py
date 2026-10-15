"""Compose an announcement (``comms:compose``): preview the counts and body, then send.

A one-time token in the form (kept in the session, consumed on send, and used as the
message's ``dedupe_key``) stops a double submit from sending twice.
"""

import secrets

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from apps.accounts.roles import require_roles
from apps.comms import announcements
from apps.comms.forms import AUDIENCE_FIELDS, ComposeForm
from apps.comms.models import Message
from apps.comms.scope import BURSAR_AUDIENCES, COMPOSE_ROLES, audiences_for

SESSION_KEY = "comms_compose_tokens"
MAX_TOKENS = 20
ALREADY_SENT = "This message was already sent."


def _issue_token(request) -> str:
    token = secrets.token_urlsafe(16)
    tokens = [t for t in request.session.get(SESSION_KEY, []) if isinstance(t, str)]
    request.session[SESSION_KEY] = (tokens + [token])[-MAX_TOKENS:]
    return token


def _token_live(request, token: str) -> bool:
    return bool(token) and token in request.session.get(SESSION_KEY, [])


def _consume_token(request, token: str) -> None:
    request.session[SESSION_KEY] = [t for t in request.session.get(SESSION_KEY, []) if t != token]


def _dedupe_key(token: str) -> str:
    return f"compose:{token}"


def _already_sent(request):
    messages.info(request, ALREADY_SENT)
    return redirect("comms:history")


def _page(request, form, preview=None):
    return render(
        request,
        "comms/compose.html",
        {"form": form, "preview": preview, "audience_fields": AUDIENCE_FIELDS},
    )


@never_cache
@require_roles(*COMPOSE_ROLES)
def compose(request):
    audiences = audiences_for(request.user)
    if not audiences:
        raise PermissionDenied
    if request.method != "POST":
        form = ComposeForm(initial={"token": _issue_token(request)}, audiences=audiences)
        return _page(request, form)

    # A bursar may only message debtors: any other audience is refused outright (403).
    if audiences == BURSAR_AUDIENCES and request.POST.get("audience") not in audiences:
        raise PermissionDenied
    action = request.POST.get("action")
    token = request.POST.get("token", "")
    data = request.POST.copy()
    if action == "send":
        if not _token_live(request, token) or (
            Message.objects.filter(
                kind=Message.Kind.ANNOUNCEMENT, dedupe_key=_dedupe_key(token)
            ).exists()
        ):
            return _already_sent(request)
    elif not _token_live(request, token):
        data["token"] = _issue_token(request)

    form = ComposeForm(data, audiences=audiences)
    preview = None
    if form.is_valid():
        assert form.chosen_audience is not None
        if action == "send":
            try:
                message = announcements.send(
                    form.chosen_audience,
                    form.students_found,
                    subject=form.cleaned_data["subject"],
                    body=form.cleaned_data["body"],
                    by=request.user,
                    dedupe_key=_dedupe_key(token),
                )
            except IntegrityError:  # the same token sent concurrently
                return _already_sent(request)
            _consume_token(request, token)
            messages.success(request, "Announcement sent.")
            return redirect("comms:message", message.pk)
        preview = announcements.preview(form.students_found, form.cleaned_data["body"])
    return _page(request, form, preview)
