import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.cache import cache

from apps.accounts.identifiers import classify_identifier
from apps.accounts.models import User


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
