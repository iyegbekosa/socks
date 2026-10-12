"""Staff import: validate each row, invite the good ones, report the bad ones.

Error messages never echo names, emails or phone numbers; nothing here logs row values.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from apps.accounts.roles import MFA_REQUIRED_ROLES, STAFF_ROLES, TEACHER
from apps.accounts.services import invite_staff
from apps.imports.models import ImportJob
from apps.imports.parsing import ImportFileError, cell_phone, cell_text, read_rows

__all__ = ["ImportFileError", "TEMPLATE_HEADERS", "import_staff", "validate_row"]

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("full_name",)
TEMPLATE_HEADERS = ("full_name", "email", "phone", "roles")
NAME_MAX = 150
PROGRESS_EVERY = 50

DUPLICATE_MESSAGE = "Email or phone already used"
MFA_MESSAGE = "This role needs a two-factor-verified admin to import"
UNEXPECTED_MESSAGE = "Unexpected error on this row"
BAD_PHONE = "Enter a valid Nigerian mobile number."
BAD_EMAIL = "Enter a valid email address."


@dataclass
class RowResult:
    invite_kwargs: dict[str, Any] | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)

    def error(self, column: str, message: str) -> None:
        self.errors.append((column, message))


def validate_row(
    row: dict[str, Any], seen_emails: set[str], seen_phones: set[str], *, verified: bool
) -> RowResult:
    """Check one row without touching the database."""
    result = RowResult()
    name = " ".join(cell_text(row.get("full_name")).split())
    if not name:
        result.error("full_name", "This column is required.")
    elif len(name) > NAME_MAX:
        result.error("full_name", f"Use at most {NAME_MAX} characters.")

    email = cell_text(row.get("email")).lower()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            result.error("email", BAD_EMAIL)
            email = ""
    phone = ""
    try:
        phone = cell_phone(row.get("phone"))
    except ValueError:
        result.error("phone", BAD_PHONE)
    if not cell_text(row.get("email")) and not cell_text(row.get("phone")):
        result.error("email", "Give an email or a phone number.")

    roles: list[str] = []
    for part in cell_text(row.get("roles")).split(","):
        role = part.strip().lower()
        if not role or role in roles:
            continue
        if role not in STAFF_ROLES:
            result.error("roles", f"Unknown role “{role[:40]}”.")
        else:
            roles.append(role)
    if not roles:
        roles = [TEACHER]
    if not verified and set(roles) & MFA_REQUIRED_ROLES:
        result.error("roles", MFA_MESSAGE)

    # Remember identifiers even for rows that fail elsewhere, so a later copy is flagged.
    duplicate = False
    if email:
        duplicate = email in seen_emails
        seen_emails.add(email)
    if phone:
        duplicate = (phone in seen_phones) or duplicate
        seen_phones.add(phone)
    if duplicate:
        result.error("email", DUPLICATE_MESSAGE)

    if result.errors:
        return result
    result.invite_kwargs = {
        "full_name": name,
        "email": email or None,
        "phone": phone or None,
        "roles": roles,
    }
    return result


class _Progress:
    def __init__(self, job: ImportJob):
        self.job = job
        self.errors: list[dict[str, Any]] = []
        self.ok = 0
        self.bad = 0

    def fail_row(self, number: int, errors: list[tuple[str, str]]) -> None:
        self.bad += 1
        self.errors.extend({"row": number, "column": c, "message": m} for c, m in errors)

    def save(self) -> None:
        job = self.job
        job.ok_rows, job.error_rows, job.errors = self.ok, self.bad, self.errors
        job.invited_count = self.ok
        job.save(update_fields=["ok_rows", "error_rows", "errors", "invited_count"])


def _validation_errors(exc: ValidationError) -> list[tuple[str, str]]:
    """Clash and format problems get fixed messages: library text could quote the value."""
    if not hasattr(exc, "error_dict"):
        return [("", DUPLICATE_MESSAGE if "already" in " ".join(exc.messages) else BAD_EMAIL)]
    out: list[tuple[str, str]] = []
    for key, messages in exc.message_dict.items():
        text = " ".join(messages)
        if "already" in text:
            out.append((key if key in ("email", "phone") else "", DUPLICATE_MESSAGE))
        elif key == "phone":
            out.append(("phone", BAD_PHONE))
        elif key == "email":
            out.append(("email", BAD_EMAIL))
        else:
            out.append(("", UNEXPECTED_MESSAGE))
    return out


def import_staff(job: ImportJob, *, verified: bool) -> None:
    """Invite every valid row of ``job``'s file; record bad rows on the job.

    Raises ``ImportFileError`` when the file as a whole cannot be imported. Each row is
    invited in its own savepoint, so one failing row never undoes the others.
    """
    with default_storage.open(job.file_name) as fileobj:
        rows = list(
            read_rows(fileobj, job.file_name, required=REQUIRED_COLUMNS, known=TEMPLATE_HEADERS)
        )
    job.total_rows = len(rows)
    job.save(update_fields=["total_rows"])

    progress = _Progress(job)
    seen_emails: set[str] = set()
    seen_phones: set[str] = set()
    try:
        for index, (number, row) in enumerate(rows, start=1):
            result = validate_row(row, seen_emails, seen_phones, verified=verified)
            if result.invite_kwargs is None:
                progress.fail_row(number, result.errors)
            else:
                try:
                    with transaction.atomic():
                        invite_staff(
                            **result.invite_kwargs,
                            by=job.created_by,
                            base_url=job.base_url,
                            verified=verified,
                        )
                except ValidationError as exc:
                    progress.fail_row(number, _validation_errors(exc))
                except IntegrityError:
                    progress.fail_row(number, [("", DUPLICATE_MESSAGE)])
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the job
                    # Type name only: exception text can carry row values.
                    logger.error(
                        "Import job %s: unexpected %s on row %s", job.pk, type(exc).__name__, number
                    )
                    progress.fail_row(number, [("", UNEXPECTED_MESSAGE)])
                else:
                    progress.ok += 1
            if index % PROGRESS_EVERY == 0:
                progress.save()
    finally:
        progress.save()
