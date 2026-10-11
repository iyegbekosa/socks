import hashlib
import hmac
import secrets
from collections.abc import Iterable

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, connection, transaction
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.identifiers import classify_identifier
from apps.accounts.models import User
from apps.accounts.roles import SCHOOL_ADMIN, set_roles


def create_student_user(
    *, admission_number: str, full_name: str, recovery_phone: str | None, password: str
) -> User:
    first, _, last = full_name.strip().partition(" ")
    user = User(
        kind=User.Kind.STUDENT,
        admission_number=admission_number,
        recovery_phone=recovery_phone,
        first_name=first,
        last_name=last.strip(),
    )
    user.set_password(password)
    user.full_clean(exclude=["password"])
    user.save()
    return user


def reset_mfa(user: User, *, by: User, request=None) -> None:
    """Delete all of ``user``'s TOTP devices so they enrol again at next login."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.audit.services import record

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
    """Staff reset codes go to their own phone; students' go to the guardian's phone."""
    phone = user.recovery_phone if user.kind == User.Kind.STUDENT else user.phone
    return phone or None


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
    """Active user matching an identifier, resolved exactly as login does."""
    kind, value = classify_identifier(raw)
    qs = User.objects.filter(is_active=True)
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
    request=None,
    send_sms: bool = True,
) -> User:
    from apps.audit.services import record

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


def update_staff_roles(user: User, roles: Iterable[str], *, by: User, request=None) -> None:
    wanted = set(roles)
    with transaction.atomic():
        admins = _lock_admins()
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
