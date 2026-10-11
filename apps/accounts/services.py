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
