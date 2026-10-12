"""Student import: validate each row, admit the good ones, report the bad ones.

Children's data: error messages never echo dates of birth, phone numbers or emails, and
nothing here logs row values.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from apps.academics.models import ClassArm, normalise_display
from apps.academics.services import current_session
from apps.accounts.identifiers import normalise_admission_number
from apps.imports.models import ImportJob
from apps.imports.parsing import ImportFileError, cell_date, cell_phone, cell_text, read_rows
from apps.students.models import RELATIONSHIP_CHOICES, Student
from apps.students.services import GuardianData, admit_student, is_admission_number_clash
from apps.tenancy.limits import PlanLimitExceeded, PlanLimits

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = (
    "first_name",
    "last_name",
    "sex",
    "date_of_birth",
    "class",
    "guardian_name",
)
TEMPLATE_HEADERS = (
    "first_name",
    "middle_name",
    "last_name",
    "sex",
    "date_of_birth",
    "class",
    "admission_number",
    "guardian_name",
    "guardian_relationship",
    "guardian_phone",
    "guardian_email",
    "guardian2_name",
    "guardian2_relationship",
    "guardian2_phone",
    "guardian2_email",
)
NAME_MAX = {"first_name": 80, "middle_name": 80, "last_name": 80, "guardian_name": 120}
SEX_VALUES = {"m": "M", "male": "M", "f": "F", "female": "F"}
RELATIONSHIPS = {key: key for key, _ in RELATIONSHIP_CHOICES} | {
    label.lower(): key for key, label in RELATIONSHIP_CHOICES
}
DEFAULT_RELATIONSHIP = "guardian"
PROGRESS_EVERY = 50

# Model/service error keys → spreadsheet columns.
FIELD_TO_COLUMN = {"arm": "class", "session": "class", "__all__": ""}
SENSITIVE_COLUMNS = {
    "date_of_birth",
    "guardian_phone",
    "guardian_email",
    "guardian2_phone",
    "guardian2_email",
}
SAFE_MESSAGES = {
    "date_of_birth": "Enter a valid date of birth.",
    "guardian_phone": "Enter a valid Nigerian mobile number.",
    "guardian2_phone": "Enter a valid Nigerian mobile number.",
    "guardian_email": "Enter a valid email address.",
    "guardian2_email": "Enter a valid email address.",
}

PLAN_LIMIT_MESSAGE = "Plan limit reached ({} students)"
CLASH_MESSAGE = "Admission number already used"
UNEXPECTED_MESSAGE = "Unexpected error on this row"
DUPLICATE_MESSAGE = "This admission number appears earlier in the file"
ALREADY_ADMITTED_MESSAGE = "Already admitted as {}"
SAME_STUDENT_MESSAGE = "This student appears earlier in the file"

StudentKey = tuple[str, str, Any]


def _name_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def student_key(first_name: str, last_name: str, date_of_birth: Any) -> StudentKey:
    """Identity used to spot re-imports: names (case-insensitive, whitespace collapsed)
    plus date of birth. Never logged."""
    return (_name_key(first_name), _name_key(last_name), date_of_birth)


def _active_students_by_key() -> dict[StudentKey, str]:
    """One query per job: active students' identity → admission number."""
    rows = Student.objects.filter(status=Student.Status.ACTIVE).values_list(
        "first_name", "last_name", "date_of_birth", "user__admission_number"
    )
    return {student_key(f, la, d): number or "" for f, la, d, number in rows}


@dataclass
class RowResult:
    admit_kwargs: dict[str, Any] | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)

    def error(self, column: str, message: str) -> None:
        self.errors.append((column, message))


def _text(row: dict[str, Any], column: str) -> str:
    return " ".join(cell_text(row.get(column)).split())


def _relationship(result: RowResult, row: dict[str, Any], column: str, default: str) -> str:
    raw = _text(row, column).lower()
    if not raw:
        return default
    value = RELATIONSHIPS.get(raw)
    if value is None:
        result.error(column, "Use mother, father, guardian or other.")
        return default
    return value


def _phone(result: RowResult, row: dict[str, Any], column: str) -> str:
    try:
        return cell_phone(row.get(column))
    except ValueError:
        result.error(column, SAFE_MESSAGES[column])
        return ""


def _email(result: RowResult, row: dict[str, Any], column: str) -> str:
    value = cell_text(row.get(column)).lower()
    if not value:
        return ""
    try:
        validate_email(value)
    except ValidationError:
        result.error(column, SAFE_MESSAGES[column])
        return ""
    return value


def validate_row(
    row: dict[str, Any], arms: dict[str, ClassArm], seen_numbers: set[str]
) -> RowResult:
    """Check one row without touching the database (except via ``arms``, built per job)."""
    result = RowResult()
    names = {}
    for column, limit in NAME_MAX.items():
        value = _text(row, column)
        if not value and column != "middle_name":
            result.error(column, "This column is required.")
        elif len(value) > limit:
            result.error(column, f"Use at most {limit} characters.")
        names[column] = value

    sex = SEX_VALUES.get(_text(row, "sex").lower())
    if sex is None:
        result.error("sex", "Use M or F.")

    dob = None
    if cell_text(row.get("date_of_birth")) == "":
        result.error("date_of_birth", "This column is required.")
    else:
        try:
            dob = cell_date(row.get("date_of_birth"))
        except ValueError:
            result.error("date_of_birth", "Use a date like 25/12/2014 (DD/MM/YYYY).")

    class_name = _text(row, "class")
    arm = arms.get(normalise_display(class_name)) if class_name else None
    if not class_name:
        result.error("class", "This column is required.")
    elif arm is None:
        result.error("class", f"No class called “{class_name[:40]}”.")

    number = normalise_admission_number(cell_text(row.get("admission_number")))
    if number:
        if number in seen_numbers:
            result.error("admission_number", DUPLICATE_MESSAGE)
        seen_numbers.add(number)

    relationship = _relationship(result, row, "guardian_relationship", DEFAULT_RELATIONSHIP)
    phone = _phone(result, row, "guardian_phone")
    email = _email(result, row, "guardian_email")
    no_contact_given = not cell_text(row.get("guardian_phone")) and not cell_text(
        row.get("guardian_email")
    )
    if no_contact_given:
        result.error("guardian_phone", "Give guardian_phone or guardian_email.")

    g2_name = _text(row, "guardian2_name")
    g2_relationship = _relationship(result, row, "guardian2_relationship", "")
    g2_phone = _phone(result, row, "guardian2_phone")
    g2_email = _email(result, row, "guardian2_email")
    if len(g2_name) > NAME_MAX["guardian_name"]:
        result.error("guardian2_name", f"Use at most {NAME_MAX['guardian_name']} characters.")
    has_second = any(
        cell_text(row.get(c))
        for c in ("guardian2_relationship", "guardian2_phone", "guardian2_email")
    )
    if has_second and not g2_name:
        result.error("guardian2_name", "Give the second guardian's name.")

    if result.errors:
        return result
    guardian2 = None
    if g2_name or has_second:
        guardian2 = GuardianData(g2_name, g2_relationship, g2_phone or None, g2_email)
    result.admit_kwargs = {
        "first_name": names["first_name"],
        "middle_name": names["middle_name"],
        "last_name": names["last_name"],
        "sex": sex,
        "date_of_birth": dob,
        "arm": arm,
        "guardian": GuardianData(names["guardian_name"], relationship, phone or None, email),
        "guardian2": guardian2,
        "admission_number": number or None,
    }
    return result


def _validation_errors(exc: ValidationError) -> list[tuple[str, str]]:
    if not hasattr(exc, "error_dict"):
        return [("", m) for m in exc.messages]
    out: list[tuple[str, str]] = []
    for key, messages in exc.message_dict.items():
        column = FIELD_TO_COLUMN.get(key, key)
        if column in SENSITIVE_COLUMNS and column != "date_of_birth":
            # Validators can quote the value; contacts get a fixed message instead.
            out.append((column, SAFE_MESSAGES[column]))
            continue
        out.extend((column, m) for m in messages)
    return out


class _Progress:
    def __init__(self, job: ImportJob):
        self.job = job
        self.errors: list[dict[str, Any]] = []
        self.ok = 0
        self.bad = 0
        self.arms: dict[int, dict[str, Any]] = {}
        self.student_ids: list[int] = []

    def fail_row(self, number: int, errors: list[tuple[str, str]]) -> None:
        self.bad += 1
        self.errors.extend({"row": number, "column": c, "message": m} for c, m in errors)

    def admitted(self, student: Student, arm: ClassArm) -> None:
        self.ok += 1
        self.student_ids.append(student.pk)
        entry = self.arms.setdefault(arm.pk, {"id": arm.pk, "name": arm.display_name, "count": 0})
        entry["count"] += 1

    def save(self) -> None:
        job = self.job
        job.ok_rows, job.error_rows, job.errors = self.ok, self.bad, self.errors
        job.arms = sorted(self.arms.values(), key=lambda a: a["name"])
        job.admitted_student_ids = list(self.student_ids)
        job.save(update_fields=["ok_rows", "error_rows", "errors", "arms", "admitted_student_ids"])


def import_students(job: ImportJob) -> None:
    """Admit every valid row of ``job``'s file; record bad rows on the job.

    Raises ``ImportFileError`` when the file as a whole cannot be imported (nothing saved).
    Each good row is admitted in its own savepoint, so one failing row never undoes others.
    """
    session = current_session()
    if session is None:
        raise ImportFileError("Set a current term before importing students.")
    with default_storage.open(job.file_name) as fileobj:
        # Fully read (and row-limit checked) before anything is admitted.
        rows = list(
            read_rows(fileobj, job.file_name, required=REQUIRED_COLUMNS, known=TEMPLATE_HEADERS)
        )
    job.total_rows = len(rows)
    job.save(update_fields=["total_rows"])

    arms = {arm.normalised_display: arm for arm in ClassArm.objects.select_related("level")}
    maximum = PlanLimits.for_current_school().plan.max_students
    progress = _Progress(job)
    seen_numbers: set[str] = set()
    admitted_by_key = _active_students_by_key()
    seen_keys: set[StudentKey] = set()
    by = job.created_by
    try:
        for index, (number, row) in enumerate(rows, start=1):
            result = validate_row(row, arms, seen_numbers)
            kw = result.admit_kwargs
            key = (
                student_key(kw["first_name"], kw["last_name"], kw["date_of_birth"]) if kw else None
            )
            if kw is None or key is None:
                progress.fail_row(number, result.errors)
            elif key in admitted_by_key:
                progress.fail_row(
                    number, [("", ALREADY_ADMITTED_MESSAGE.format(admitted_by_key[key]))]
                )
            elif key in seen_keys:
                progress.fail_row(number, [("", SAME_STUDENT_MESSAGE)])
            else:
                seen_keys.add(key)
                try:
                    with transaction.atomic():
                        student, _ = admit_student(
                            **kw,
                            session=session,
                            consent_method=Student.ConsentMethod.PAPER_FORM,
                            issue_password=False,
                            by=by,
                        )
                except PlanLimitExceeded:
                    progress.fail_row(number, [("", PLAN_LIMIT_MESSAGE.format(maximum))])
                except ValidationError as exc:
                    progress.fail_row(number, _validation_errors(exc))
                except IntegrityError as exc:
                    if not is_admission_number_clash(exc):
                        _log_unexpected(job, number, exc)
                        progress.fail_row(number, [("", UNEXPECTED_MESSAGE)])
                    else:
                        progress.fail_row(number, [("admission_number", CLASH_MESSAGE)])
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the job
                    _log_unexpected(job, number, exc)
                    progress.fail_row(number, [("", UNEXPECTED_MESSAGE)])
                else:
                    progress.admitted(student, kw["arm"])
                    admitted_by_key[key] = student.user.admission_number or ""
            if index % PROGRESS_EVERY == 0:
                progress.save()
    finally:
        # Also on an escaping exception: the counts must match what was saved.
        progress.save()


def _log_unexpected(job: ImportJob, number: int, exc: BaseException) -> None:
    # Type name only: exception text and tracebacks can carry row values (children's data).
    logger.error("Import job %s: unexpected %s on row %s", job.pk, type(exc).__name__, number)
