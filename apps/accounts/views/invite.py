from django.contrib import messages
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts.models import User
from apps.audit.services import record

INVITE_URL_TOKEN = "set-password"
SESSION_KEY = "_invite_token"


def _invalid(request):
    response = render(request, "accounts/invite_invalid.html", status=400)
    response["Referrer-Policy"] = "no-referrer"
    return response


def _get_user(uidb64: str) -> User | None:
    try:
        pk = urlsafe_base64_decode(uidb64).decode()
        return User.objects.filter(pk=pk).first()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None


def _usable(user: User | None, token: str) -> bool:
    return bool(
        user is not None
        and user.kind == User.Kind.STAFF
        and user.is_active
        and not user.has_usable_password()
        and default_token_generator.check_token(user, token)
    )


@sensitive_post_parameters()
@never_cache
def invite_accept_view(request, uidb64: str, token: str):
    user = _get_user(uidb64)
    if token == INVITE_URL_TOKEN:
        # Second step: the real token lives in the session, not in the URL.
        if not _usable(user, request.session.get(SESSION_KEY, "")):
            return _invalid(request)
    else:
        if not _usable(user, token):
            return _invalid(request)
        # Park the token in the session and drop it from the URL so it does not
        # linger in history or Referer headers (as PasswordResetConfirmView does).
        request.session[SESSION_KEY] = token
        redirect_response = redirect(
            reverse("accounts:invite_accept", args=[uidb64, INVITE_URL_TOKEN])
        )
        redirect_response["Referrer-Policy"] = "no-referrer"
        return redirect_response

    assert user is not None
    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            request.session.pop(SESSION_KEY, None)
            record("user.invite_accepted", request=request, actor=user, obj=user)
            messages.success(request, "Your password is set. Log in to continue.")
            return redirect(reverse("accounts:login"))
    else:
        form = SetPasswordForm(user)
    response = render(request, "accounts/invite_accept.html", {"form": form, "invitee": user})
    response["Referrer-Policy"] = "no-referrer"
    return response
