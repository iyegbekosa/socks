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
