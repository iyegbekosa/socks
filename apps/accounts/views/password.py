from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts.identifiers import classify_identifier
from apps.accounts.models import User
from apps.accounts.services import reset_eligible, reset_link_token_generator
from apps.accounts.views.login import forget_login_lockout
from apps.audit.services import record
from apps.core.request_id import client_ip

RESET_URL_TOKEN = "set"
SESSION_KEY = "_reset_link"
SAME_PASSWORD = "Choose a password different from your current one."


class ChangeForm(PasswordChangeForm):
    def clean_new_password1(self):
        new = self.cleaned_data["new_password1"]
        if self.user.check_password(new):
            raise ValidationError(SAME_PASSWORD, code="password_unchanged")
        return new


@sensitive_post_parameters()
@never_cache
@login_required
def password_change_view(request):
    user = request.user
    form = ChangeForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user.must_change_password = False  # saved together with the new hash
        form.save()
        update_session_auth_hash(request, user)
        record("auth.password_changed", request=request, actor=user, obj=user)
        messages.success(request, "Your password has been changed.")
        return redirect(reverse("home"))
    return render(
        request,
        "accounts/password_change.html",
        {"form": form, "forced": user.must_change_password},
    )


def _invalid(request):
    response = render(request, "accounts/reset_link_invalid.html", status=400)
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
        reset_eligible(user)  # pending staff invitees use their invite instead
        and user is not None
        and reset_link_token_generator.check_token(user, token)
    )


def _forget_lockout(user: User, ip: str | None) -> None:
    # A locked-out user must be able to use the new password straight away.
    for raw in (user.admission_number, user.email, user.phone):
        if raw:
            forget_login_lockout(classify_identifier(raw)[1], ip)


@sensitive_post_parameters()
@never_cache
def reset_link_confirm_view(request, uidb64: str | None = None, token: str | None = None):
    if token is None:
        # Second step (token-less URL): the credential lives in the session.
        stored = request.session.get(SESSION_KEY)
        if not isinstance(stored, dict):
            return _invalid(request)
        uidb64, token = stored.get("uid", ""), stored.get("token", "")
        user = _get_user(uidb64)
        if not _usable(user, token):
            return _invalid(request)
    else:
        user = _get_user(uidb64 or "")
        if not _usable(user, token):
            return _invalid(request)
        request.session[SESSION_KEY] = {"uid": uidb64, "token": token}
        moved = redirect(reverse("accounts:reset_link_set"))
        moved["Referrer-Policy"] = "no-referrer"
        return moved

    assert user is not None
    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            user.must_change_password = False
            form.save()  # new hash also invalidates this token (and any outstanding ones)
            request.session.pop(SESSION_KEY, None)
            pending = request.session.get("mfa_pending")
            if isinstance(pending, dict) and pending.get("user_id") == user.pk:
                request.session.pop("mfa_pending", None)
            _forget_lockout(user, client_ip(request))
            record("auth.password_reset", request=request, actor=user, obj=user)
            messages.success(
                request, "Your password has been changed. Log in with the new password."
            )
            done = redirect(reverse("accounts:login"))
            done["Referrer-Policy"] = "no-referrer"
            return done
    else:
        form = SetPasswordForm(user)
    response = render(request, "accounts/reset_link_confirm.html", {"form": form, "target": user})
    response["Referrer-Policy"] = "no-referrer"
    return response
