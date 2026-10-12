"""One-time passwords for student logins (shown once, stored only as a hash)."""

import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db import transaction

if TYPE_CHECKING:
    from apps.students.models import Student

# No 0/O, 1/I: easy to read off a printed sheet.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 10


def generate_one_time_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


@dataclass(frozen=True)
class IssuedPassword:
    student: "Student"
    admission_number: str
    password: str = field(repr=False)  # never in reprs or logs


def issue_one_time_passwords(
    students: Iterable["Student"], *, by: Any, request: Any = None
) -> list[IssuedPassword]:
    """Give each student a fresh one-time password (only the hash is stored) and force a
    change at next login. Re-issuing invalidates any earlier password. Audited per student
    without the password."""
    from apps.audit.services import record

    issued: list[IssuedPassword] = []
    with transaction.atomic():
        for student in students:
            user = student.user
            password = generate_one_time_password()
            user.set_password(password)
            user.must_change_password = True
            user.save(update_fields=["password", "must_change_password"])
            record("student.password_issued", request=request, actor=by, obj=student)
            issued.append(IssuedPassword(student, user.admission_number or "", password))
    return issued
