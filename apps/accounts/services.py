import hashlib
import hmac
import secrets
from collections.abc import Iterable

from django.conf import settings
from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import PasswordResetTokenGenerator, default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, connection, transaction
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import base36_to_int, urlsafe_base64_encode

from apps.accounts.identifiers import classify_identifier
from apps.accounts.models import User
from apps.accounts.roles import MFA_REQUIRED_ROLES, SCHOOL_ADMIN, set_roles

MFA_VERIFICATION_REQUIRED = "This change needs a two-factor-verified admin."


class MfaVerificationRequired(ValidationError):
    """An MFA-sensitive staff change was attempted from a session without a verified OTP."""

    def __init__(self) -> None:
        super().__init__(MFA_VERIFICATION_REQUIRED)


def _require_verified(verified: bool) -> None:
    # Granting/removing MFA roles, inviting into them and resetting MFA must come from a
    # session that passed a TOTP check, or an MFA-free proprietor could escalate to admin.
    if not verified:
        raise MfaVerificationRequired()


def create_student_user(
    *, admission_number: str, first_name: str, last_name: str, password: str | None
) -> User:
    """Internal helper for ``apps.students.services.admit_student``: the student's login.

    ``password`` is a one-time password (stored hashed); ``None`` leaves the password
    unusable until one is issued. Either way the student must change it at first login.
    """
    user = User(
        kind=User.Kind.STUDENT,
        admission_number=admission_number,
        first_name=first_name,
        last_name=last_name,
        must_change_password=True,
    )
    if password is None:
        user.set_unusable_password()
    else:
        user.set_password(password)
    user.full_clean(exclude=["password"])
    user.save()
    return user


def reset_mfa(user: User, *, by: User, verified: bool, request=None) -> None:
    """Delete all of ``user``'s TOTP devices so they enrol again at next login.

    ``verified``: the acting session passed a TOTP check (``request.user.is_verified()``).
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.audit.services import record

    _require_verified(verified)
    TOTPDevice.objects.filter(user=user).delete()
    record("auth.mfa_reset", request=request, actor=by, obj=user)


RESET_CODE_TTL = 600
RESET_MAX_ATTEMPTS = 5


def _reset_key(user: User) -> str:
    return f"reset:code:{user.pk}"


def _attempts_key(user: User) -> str:
    return f"reset:attempts:{user.pk}"


def _code_digest(user: User, code: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{user.pk}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def reset_phone_for(user: User) -> str | None:
    """Staff reset codes go to their own phone; students' go to the main guardian's phone."""
    if user.kind == User.Kind.STUDENT:
        student = getattr(user, "student", None)  # reverse OneToOne; None without a profile
        phone = student.guardian_phone if student is not None else None
    else:
        phone = user.phone
    return phone or None


def reset_email_for(user: User) -> str | None:
    """Reset links go to the main guardian's email (else the second guardian's) for students,
    and to the staff member's own email for staff."""
    if user.kind == User.Kind.STUDENT:
        student = getattr(user, "student", None)
        email = (student.guardian_email or student.guardian2_email) if student is not None else ""
    else:
        email = user.email
    return email or None


RESET_LINK_MAX_AGE = 3600


class ResetLinkTokenGenerator(PasswordResetTokenGenerator):
    """Tokens for emailed reset links.

    * A different ``key_salt`` from the invite generator, so an invite token never validates
      as a reset token and vice versa (the HMAC differs).
    * One hour maximum age, enforced here and independent of ``PASSWORD_RESET_TIMEOUT``
      (72 h, which invites rely on). ``super().check_token`` verifies the signature, the
      72 h bound and that the password/last_login are unchanged (so a link works once);
      we then decode the token's own timestamp and apply the shorter bound.
    """

    key_salt = "build2learn.reset-link"

    def check_token(self, user, token):  # type: ignore[no-untyped-def]
        if not super().check_token(user, token):
            return False
        try:
            ts = base36_to_int(token.split("-")[0])
        except ValueError:
            return False
        return (self._num_seconds(self._now()) - ts) <= RESET_LINK_MAX_AGE


reset_link_token_generator = ResetLinkTokenGenerator()


def reset_link(user: User, base_url: str) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = reset_link_token_generator.make_token(user)
    return f"{base_url.rstrip('/')}/accounts/reset/link/{uid}/{token}/"


def send_reset_link_email(user: User, email: str, base_url: str) -> None:
    """Email the reset link. The link is a credential: never logged or audited."""
    student = getattr(user, "student", None) if user.kind == User.Kind.STUDENT else None
    first_name = user.first_name or "your account"
    context = {
        "school_name": connection.tenant.name,  # type: ignore[attr-defined]
        "first_name": first_name,
        "name": student.full_name if student is not None else user.get_full_name() or first_name,
        "admission_number": user.admission_number if user.kind == User.Kind.STUDENT else None,
        "link": reset_link(user, base_url),
    }
    send_mail(
        render_to_string("accounts/email/reset_link_subject.txt", context).strip(),
        render_to_string("accounts/email/reset_link_body.txt", context),
        None,
        [email],
    )


def issue_reset_code(user: User) -> str | None:
    """Create a fresh code (replacing any earlier one); None if there is nowhere to send it."""
    if reset_phone_for(user) is None:
        return None
    code = f"{secrets.randbelow(10**6):06d}"
    cache.set(_reset_key(user), _code_digest(user, code), RESET_CODE_TTL)
    # The per-user verify limiter (views) is deliberately NOT reset here.
    # Attempts live in their own key so the increment is atomic (no read-modify-write race).
    cache.set(_attempts_key(user), 0, RESET_CODE_TTL)
    return code


def check_reset_code(user: User, code: str) -> bool:
    """Count the attempt first, then compare in constant time. Does not consume the code;
    call clear_reset_code (consume) once the reset has actually happened."""
    digest = cache.get(_reset_key(user))
    if digest is None:
        return False
    try:
        attempts = cache.incr(_attempts_key(user))
    except ValueError:
        return False
    if attempts > RESET_MAX_ATTEMPTS:
        consume_reset_code(user)
        return False
    return hmac.compare_digest(digest, _code_digest(user, (code or "").strip()))


def consume_reset_code(user: User) -> None:
    cache.delete_many([_reset_key(user), _attempts_key(user)])


def find_user_by_identifier(raw: str) -> User | None:
    """Active user matching an identifier, resolved exactly as login does, for password reset.

    Pending invitees (unusable password) are excluded: they set their first password via the
    invite link, so a reset code must not become a second way in.
    """
    kind, value = classify_identifier(raw)
    qs = User.objects.filter(is_active=True).exclude(password__startswith=UNUSABLE_PASSWORD_PREFIX)
    if kind == "email":
        qs = qs.filter(kind="staff", email=value)
    elif kind == "phone":
        qs = qs.filter(kind="staff", phone=value)
    else:
        qs = qs.filter(kind="student", admission_number=value)
    return qs.first()


# --- Staff invites and management -------------------------------------------------


def invite_link(user: User, base_url: str) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"{base_url.rstrip('/')}/accounts/invite/{uid}/{token}/"


def _send_invite_on_commit(user: User, base_url: str, *, send_sms: bool = True) -> None:
    """Queue the invite email/SMS for after the surrounding transaction commits."""
    from apps.core import sms

    school_name = connection.tenant.name  # type: ignore[attr-defined]
    link = invite_link(user, base_url)
    email, phone = user.email, user.phone

    def _send() -> None:
        if email:
            send_mail(
                render_to_string(
                    "accounts/email/invite_subject.txt", {"school_name": school_name}
                ).strip(),
                render_to_string(
                    "accounts/email/invite_body.txt",
                    {"school_name": school_name, "link": link, "name": user.first_name},
                ),
                None,
                [email],
            )
        if send_sms and phone and sms.sms_configured():
            sms.send_sms(
                phone,
                f"{school_name}: you've been invited to the staff portal. "
                f"Set your password: {link}",
            )

    transaction.on_commit(_send)


def invite_staff(
    *,
    full_name: str,
    email: str | None,
    phone: str | None,
    roles: Iterable[str],
    by: User | None,
    base_url: str,
    verified: bool,
    request=None,
    send_sms: bool = True,
) -> User:
    """``verified``: the acting session passed a TOTP check; required for MFA roles.
    Platform provisioning (``by=None``) passes True."""
    from apps.audit.services import record

    if set(roles) & MFA_REQUIRED_ROLES:
        _require_verified(verified)
    email = (email or "").strip().lower()
    phone = (phone or "").strip() or None
    if not email and not phone:
        raise ValidationError("An email address or a phone number is required.")
    first, _, last = full_name.strip().partition(" ")
    user = User(
        kind=User.Kind.STAFF,
        first_name=first,
        last_name=last.strip(),
        email=email,
        phone=phone,
    )
    user.set_unusable_password()
    user._normalise()  # canonical phone/email; invalid phones raise ValidationError
    roles = sorted(set(roles))
    errors = {}
    clash = User.objects.filter(kind=User.Kind.STAFF)
    if user.email and clash.filter(email=user.email).exists():
        errors["email"] = "A staff member with this email already exists."
    if user.phone and clash.filter(phone=user.phone).exists():
        errors["phone"] = "A staff member with this phone number already exists."
    if errors:
        raise ValidationError(errors)
    user.full_clean(exclude=["password"], validate_constraints=True)
    with transaction.atomic():
        try:
            with transaction.atomic():  # savepoint: keep the outer transaction usable
                user.save()
        except IntegrityError:
            # A concurrent invite for the same identifier won the race.
            raced = {}
            if user.email and clash.filter(email=user.email).exists():
                raced["email"] = "A staff member with this email already exists."
            if user.phone and clash.filter(phone=user.phone).exists():
                raced["phone"] = "A staff member with this phone number already exists."
            raise ValidationError(
                raced or {"email": "A staff member with these details already exists."}
            ) from None
        set_roles(user, roles, by=by, request=request)
        record(
            "user.invited",
            request=request,
            actor=by,
            obj=user,
            after={"roles": roles},
        )
        _send_invite_on_commit(user, base_url, send_sms=send_sms)
    return user


def resend_invite(
    user: User, *, by: User | None, base_url: str, request=None, send_sms: bool = True
) -> None:
    from apps.audit.services import record

    if user.kind != User.Kind.STAFF or not user.is_active or user.has_usable_password():
        raise ValidationError("An invite can only be resent while the password is not set.")
    with transaction.atomic():
        record("user.invited", request=request, actor=by, obj=user, after={"resent": True})
        _send_invite_on_commit(user, base_url, send_sms=send_sms)


def _lock_admins() -> list[int]:
    """Active admins with a usable password, serialised via a lock on the role's group row."""
    Group.objects.select_for_update().get_or_create(name=SCHOOL_ADMIN)
    return list(
        User.objects.select_for_update(of=("self",))
        .filter(is_active=True, kind=User.Kind.STAFF, groups__name=SCHOOL_ADMIN)
        .exclude(password__startswith="!")  # only admins who have accepted their invite
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def update_staff_roles(
    user: User, roles: Iterable[str], *, by: User, verified: bool, request=None
) -> None:
    """``verified``: the acting session passed a TOTP check; required to add or remove any
    MFA role (school_admin, principal, bursar)."""
    wanted = set(roles)
    with transaction.atomic():
        admins = _lock_admins()
        if (set(user.roles) ^ wanted) & MFA_REQUIRED_ROLES:
            _require_verified(verified)
        if SCHOOL_ADMIN not in wanted and user.pk in admins and len(admins) <= 1:
            raise ValidationError(
                "This is the last active school administrator. Make someone else an "
                "administrator first."
            )
        set_roles(user, wanted, by=by, request=request)


def deactivate_staff(user: User, *, by: User, request=None) -> None:
    from apps.audit.services import record

    if user.pk == by.pk:
        raise ValidationError("You cannot deactivate yourself.")
    if not user.is_active:
        return
    with transaction.atomic():
        admins = _lock_admins()
        if user.pk in admins and len(admins) <= 1:
            raise ValidationError(
                "This is the last active school administrator and cannot be deactivated."
            )
        user.is_active = False
        user.save(update_fields=["is_active"])
        record("user.deactivated", request=request, actor=by, obj=user)


def reactivate_staff(user: User, *, by: User, request=None) -> None:
    from apps.audit.services import record

    if user.is_active:
        return
    with transaction.atomic():
        user.is_active = True
        user.save(update_fields=["is_active"])
        record("user.reactivated", request=request, actor=by, obj=user)
