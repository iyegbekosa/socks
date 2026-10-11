import re
from datetime import date
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils import timezone
from django.utils.text import slugify
from django_tenants.utils import get_tenant_model

from apps.academics.models import (
    PREFIX_RE,
    SECTIONS,
    AcademicSession,
    AcademicSettings,
    AdmissionCounter,
    ClassArm,
    ClassLevel,
    FormTeacher,
    Subject,
    SubjectOffering,
    Term,
)
from apps.accounts.identifiers import looks_like_phone, normalise_admission_number
from apps.audit.services import record

NAME_RE = re.compile(r"^(\d{4})/(\d{4})$")
ACADEMICS_LOCK_KEY = 0x61636164656D6963  # "academic" as a bigint


def academic_write_lock() -> None:
    """Serialise session/term writes for this school until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [ACADEMICS_LOCK_KEY])


def _term_info(term: Term) -> dict:
    return {"session": term.session.name, "number": term.number, "term_id": term.pk}


def validate_session(name: str, terms: list[tuple[date, date]]) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}

    def add(key: str, msg: str) -> None:
        errors.setdefault(key, []).append(msg)

    match = NAME_RE.match(name or "")
    if not match or int(match.group(2)) != int(match.group(1)) + 1:
        add(
            "name",
            "Session name must be like 2026/2027 (the second year is one more than the first).",
        )
    elif AcademicSession.objects.filter(name=name).exists():
        add("name", f"Session {name} already exists.")
    if len(terms) != 3:
        add("terms", "A session needs exactly three terms.")
        return errors
    for i, (start, end) in enumerate(terms, start=1):
        if end <= start:
            add(
                f"term_{i}",
                f"Term {i} must end after it starts; the end date is on or before the start.",
            )
        elif i > 1 and start <= terms[i - 2][1]:
            add(f"term_{i}", f"Term {i} must start after term {i - 1} ends.")
    if match and not errors.get("name"):
        first = int(match.group(1))
        if terms[0][0].year != first:
            add("term_1", f"The session starts in {first}, so term 1 must start in {first}.")
        if terms[2][1].year != first + 1:
            add("term_3", f"The session ends in {first + 1}, so term 3 must end in {first + 1}.")
    if not errors:
        s_start, s_end = terms[0][0], terms[2][1]
        if AcademicSession.objects.filter(start_date__lte=s_end, end_date__gte=s_start).exists():
            add("name", "These dates overlap an existing session.")
    return errors


def create_session(
    *, name: str, terms: list[tuple[date, date]], by: Any, request: Any = None
) -> AcademicSession:
    name = (name or "").strip()
    with transaction.atomic():
        academic_write_lock()
        errors = validate_session(name, terms)
        if errors:
            raise ValidationError(errors)
        session = AcademicSession(name=name, start_date=terms[0][0], end_date=terms[2][1])
        session.full_clean()
        session.save()
        created = []
        for number, (start, end) in enumerate(terms, start=1):
            term = Term(session=session, number=number, start_date=start, end_date=end)
            term.full_clean(exclude=["is_current"])
            term.save()
            created.append(term)
        record(
            "academics.session_created",
            request=request,
            actor=by,
            obj=session,
            after={
                "name": session.name,
                "terms": [{"number": t.number, "term_id": t.pk} for t in created],
            },
        )
    return session


def set_current_term(term: Term, *, by: Any, request: Any = None) -> None:
    with transaction.atomic():
        academic_write_lock()
        old = list(
            Term.objects.select_for_update().select_related("session").filter(is_current=True)
        )
        for previous in old:
            previous.is_current = False
            previous.save(update_fields=["is_current"])
        term.is_current = True
        term.save(update_fields=["is_current"])
        term = Term.objects.select_related("session").get(pk=term.pk)
        record(
            "academics.term_set_current",
            request=request,
            actor=by,
            obj=term,
            before=_term_info(old[0]) if old else None,
            after=_term_info(term),
        )


def current_term() -> Term | None:
    return Term.objects.filter(is_current=True).select_related("session").first()


def current_session() -> AcademicSession | None:
    term = current_term()
    return term.session if term else None


# --- class structure and subjects ---------------------------------------------


def _audit_structure(what: str, obj: Any, *, by: Any, request: Any, pk: int | None = None) -> None:
    record(
        "academics.structure_changed",
        request=request,
        actor=by,
        obj=obj,
        after={"what": what, "id": pk if pk is not None else obj.pk},
    )


def create_arm(
    *, level: ClassLevel, suffix: str, stream: str | None = None, by: Any, request: Any = None
) -> ClassArm:
    with transaction.atomic():
        academic_write_lock()
        arm = ClassArm(level=level, suffix=(suffix or "").strip(), stream=stream or None)
        arm.full_clean()
        arm.save()
        _audit_structure("arm_created", arm, by=by, request=request)
    return arm


def rename_arm(
    arm: ClassArm,
    *,
    suffix: str,
    stream: str | None = None,
    by: Any,
    request: Any = None,
) -> ClassArm:
    with transaction.atomic():
        academic_write_lock()
        candidate = ClassArm(
            pk=arm.pk, level=arm.level, suffix=(suffix or "").strip(), stream=stream or None
        )
        candidate._state.adding = False
        candidate.full_clean()
        arm.suffix, arm.stream = candidate.suffix, candidate.stream
        arm.save(update_fields=["suffix", "stream"])
        _audit_structure("arm_renamed", arm, by=by, request=request)
    return arm


def delete_arm(arm: ClassArm, *, by: Any, request: Any = None) -> None:
    pk = arm.pk
    try:
        with transaction.atomic():
            academic_write_lock()
            label = arm.display_name
            arm.delete()
            record(
                "academics.structure_changed",
                request=request,
                actor=by,
                after={"what": "arm_deleted", "id": pk},
            )
    except (ProtectedError, RestrictedError) as exc:
        raise ValidationError(
            f"{label} has enrolments or teaching assignments, so it cannot be deleted. "
            "Move or withdraw the students and remove its assignments first."
        ) from exc


def _clean_sections(sections: Any) -> list[str]:
    values = list(sections or [])
    if not values or any(v not in SECTIONS for v in values):
        raise ValidationError({"sections": "Choose at least one valid section."})
    return [s for s in SECTIONS if s in values]


def _other_subjects(exclude_pk: int | None) -> Any:
    qs = Subject.objects.all()
    return qs.exclude(pk=exclude_pk) if exclude_pk is not None else qs


def _subject_code(name: str, exclude_pk: int | None = None) -> str:
    base = slugify(name)[:80] or "subject"
    code, n = base, 2
    while _other_subjects(exclude_pk).filter(code=code).exists():
        code, n = f"{base}-{n}", n + 1
    return code


def _check_subject_name(name: str, exclude_pk: int | None = None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValidationError({"name": "Subject name is required."})
    if _other_subjects(exclude_pk).filter(name__iexact=name).exists():
        raise ValidationError({"name": f"{name} already exists."})
    return name


def create_subject(*, name: str, sections: Any, by: Any, request: Any = None) -> Subject:
    with transaction.atomic():
        academic_write_lock()
        name = _check_subject_name(name)
        subject = Subject(name=name, code=_subject_code(name), sections=_clean_sections(sections))
        subject.full_clean()
        subject.save()
        _audit_structure("subject_created", subject, by=by, request=request)
    return subject


def update_subject(
    subject: Subject, *, name: str, sections: Any, by: Any, request: Any = None
) -> Subject:
    with transaction.atomic():
        academic_write_lock()
        new_name = _check_subject_name(name, subject.pk)
        new_sections = _clean_sections(sections)
        candidate = Subject(pk=subject.pk, name=new_name, code=subject.code, sections=new_sections)
        candidate._state.adding = False
        candidate.full_clean()
        subject.name, subject.sections = new_name, new_sections
        subject.save(update_fields=["name", "sections"])
        _audit_structure("subject_updated", subject, by=by, request=request)
    return subject


def delete_subject(subject: Subject, *, by: Any, request: Any = None) -> None:
    pk, label = subject.pk, subject.name
    try:
        with transaction.atomic():
            academic_write_lock()
            subject.delete()
            record(
                "academics.structure_changed",
                request=request,
                actor=by,
                after={"what": "subject_deleted", "id": pk},
            )
    except (ProtectedError, RestrictedError) as exc:
        raise ValidationError(
            f"{label} has offerings, so it cannot be deleted. Remove its offerings first."
        ) from exc


# --- offerings and form teachers ------------------------------------------------


def _check_teacher(teacher: Any) -> None:
    ok = (
        teacher is not None
        and teacher.is_active
        and teacher.kind == "staff"
        and teacher.has_role("teacher")
    )
    if not ok:
        raise ValidationError({"teacher": "Choose an active staff member who is a teacher."})


def check_session_open(session: AcademicSession) -> None:
    if session.end_date < timezone.localdate():
        raise ValidationError(
            {"session": f"Session {session.name} has ended, so it can no longer be changed."}
        )


def assign_offering(
    *,
    subject: Subject,
    arm: ClassArm,
    session: AcademicSession,
    teacher: Any,
    is_compulsory: bool = True,
    by: Any,
    request: Any = None,
) -> SubjectOffering:
    """Create or update the offering of a subject in an arm for a session."""
    with transaction.atomic():
        academic_write_lock()
        if teacher is not None:
            _check_teacher(teacher)
        check_session_open(session)
        if arm.level.section not in (subject.sections or []):
            raise ValidationError(
                {"subject": f"{subject.name} is not taught in {arm.level.get_section_display()}."}
            )
        offering, _ = SubjectOffering.objects.update_or_create(
            subject=subject,
            arm=arm,
            session=session,
            defaults={"teacher": teacher, "is_compulsory": is_compulsory},
        )
        record(
            "academics.offering_assigned",
            request=request,
            actor=by,
            obj=offering,
            after={
                "what": "offering",
                "subject_id": subject.pk,
                "arm_id": arm.pk,
                "session_id": session.pk,
                "teacher_id": teacher.pk if teacher else None,
            },
        )
    return offering


def assign_form_teacher(
    *, arm: ClassArm, session: AcademicSession, teacher: Any, by: Any, request: Any = None
) -> FormTeacher:
    with transaction.atomic():
        academic_write_lock()
        _check_teacher(teacher)
        check_session_open(session)
        form_teacher, _ = FormTeacher.objects.update_or_create(
            arm=arm, session=session, defaults={"teacher": teacher}
        )
        record(
            "academics.form_teacher_assigned",
            request=request,
            actor=by,
            obj=form_teacher,
            after={
                "what": "form_teacher",
                "subject_id": None,
                "arm_id": arm.pk,
                "session_id": session.pk,
                "teacher_id": teacher.pk,
            },
        )
    return form_teacher


def remove_form_teacher(
    *, arm: ClassArm, session: AcademicSession, by: Any, request: Any = None
) -> None:
    with transaction.atomic():
        academic_write_lock()
        check_session_open(session)
        deleted, _ = FormTeacher.objects.filter(arm=arm, session=session).delete()
        if deleted:
            record(
                "academics.form_teacher_assigned",
                request=request,
                actor=by,
                after={
                    "what": "form_teacher",
                    "subject_id": None,
                    "arm_id": arm.pk,
                    "session_id": session.pk,
                    "teacher_id": None,
                },
            )


# --- Admission numbers -------------------------------------------------------

ADMISSION_MAX_LEN = 32
_LITERAL_RE = re.compile(r"^[A-Z0-9/\-]*$")
_TOKEN_RE = re.compile(r"\{(PREFIX|YEAR|SEQ:(\d+))\}")
_PREFIX_RE = re.compile(PREFIX_RE)
PHONE_SHAPED_MESSAGE = "That would look like a phone number, which students could not log in with."


def default_prefix() -> str:
    """Initials of the current school's name, letters only, at most 8 characters."""
    tenant = getattr(connection, "tenant", None)
    name = getattr(tenant, "name", "") or ""
    if not name:
        # Celery TenantTask and schema_context use a FakeTenant with no name: look the school up.
        schema = getattr(connection, "schema_name", None)
        school = get_tenant_model().objects.filter(schema_name=schema).first() if schema else None
        name = getattr(school, "name", "") or ""
    initials = "".join(
        ch for word in name.split() for ch in word[:1] if ch.isalpha() and ch.isascii()
    )
    return initials.upper()[:8] or "STU"


def get_academic_settings() -> AcademicSettings:
    obj, _ = AcademicSettings.objects.get_or_create(pk=1)
    if not obj.admission_prefix:
        obj.admission_prefix = default_prefix()
        obj.save(update_fields=["admission_prefix"])
    return obj


def academic_settings_or_defaults() -> AcademicSettings:
    """Read-only view of the settings: an unsaved instance with defaults if no row exists."""
    obj = AcademicSettings.objects.filter(pk=1).first()
    if obj is None:
        obj = AcademicSettings(pk=1)
    if not obj.admission_prefix:
        obj.admission_prefix = default_prefix()
    return obj


def render_admission_number(pattern: str, prefix: str, year: int, seq: int) -> str:
    def sub(match: re.Match) -> str:
        token = match.group(1)
        if token == "PREFIX":
            return prefix
        if token == "YEAR":
            return str(year)
        return str(seq).zfill(int(match.group(2)))

    return _TOKEN_RE.sub(sub, pattern)


def validate_admission_prefix(prefix: str) -> None:
    if not _PREFIX_RE.match(prefix or ""):
        raise ValidationError({"admission_prefix": "Use 1 to 8 capital letters, A to Z only."})


def _check_phone_safe(pattern: str, fail) -> None:
    """Reject patterns that can render a phone-shaped number at some sequence value.

    A letter (literal or {PREFIX}) or a "/" makes a number unparseable as a phone. Otherwise
    the result can be all digits, so its start must be fixed text (not {SEQ}) that cannot
    begin a Nigerian number (0..., 234...).
    """
    if "{PREFIX}" in pattern or re.search(r"[A-Z/]", _TOKEN_RE.sub("", pattern)):
        return
    fixed = ""
    for part in re.split(r"(\{[^}]*\})", pattern):
        if part.startswith("{SEQ"):
            break
        fixed += "2026" if part == "{YEAR}" else part.replace("-", "")
    if len(fixed) < 3:
        raise fail(
            "Start the pattern with a prefix, letters, or at least three fixed digits "
            "(such as {YEAR}) so numbers cannot look like phone numbers."
        )
    if fixed.startswith(("0", "234")):
        raise fail("Admission numbers must not start with 0 or 234; they could look like phones.")


def validate_admission_pattern(pattern: str, prefix: str) -> None:
    def fail(msg: str) -> ValidationError:
        return ValidationError({"admission_pattern": msg})

    pattern = pattern or ""
    seqs = [m for m in _TOKEN_RE.finditer(pattern) if m.group(2) is not None]
    if len(seqs) != 1:
        raise fail("The pattern must contain {SEQ:4} (a sequence number) exactly once.")
    if not 3 <= int(seqs[0].group(2)) <= 6:
        raise fail("The sequence number must be 3 to 6 digits, for example {SEQ:4}.")
    literal = _TOKEN_RE.sub("", pattern)
    if not _LITERAL_RE.match(literal):
        raise fail("Use only capital letters, digits, / and -, plus {PREFIX}, {YEAR} and {SEQ:n}.")
    _check_phone_safe(pattern, fail)
    sample = render_admission_number(pattern, prefix, 2026, 1)
    if len(sample) > ADMISSION_MAX_LEN:
        raise fail(f"Admission numbers can be at most {ADMISSION_MAX_LEN} characters long.")
    if looks_like_phone(sample):
        raise fail(PHONE_SHAPED_MESSAGE)


def _admission_number_used(number: str) -> bool:
    return get_user_model().objects.filter(admission_number=number).exists()


def validate_manual_admission_number(raw: str) -> str:
    number = normalise_admission_number(raw)
    if not number:
        raise ValidationError("Enter an admission number.")
    if len(number) > ADMISSION_MAX_LEN:
        raise ValidationError(f"Admission numbers can be at most {ADMISSION_MAX_LEN} characters.")
    if not re.fullmatch(r"[A-Z0-9/\-]+", number):
        raise ValidationError("Use only letters, digits, / and - in an admission number.")
    if looks_like_phone(number):
        raise ValidationError(PHONE_SHAPED_MESSAGE)
    if _admission_number_used(number):
        raise ValidationError("That admission number is already in use.")
    return number


def next_admission_number(year: int) -> str:
    """Allocate the next free admission number for `year`. Call inside the caller's atomic block."""
    if not connection.in_atomic_block:
        raise RuntimeError("next_admission_number must be called inside transaction.atomic().")
    conf = get_academic_settings()
    AdmissionCounter.objects.get_or_create(year=year)  # savepoint-safe against the create race
    counter = AdmissionCounter.objects.select_for_update().get(year=year)
    while True:
        counter.last += 1
        number = render_admission_number(
            conf.admission_pattern, conf.admission_prefix, year, counter.last
        )
        if len(number) > ADMISSION_MAX_LEN or looks_like_phone(number):
            raise ValidationError(
                "The admission-number pattern has run out of room; "
                "ask the school admin to change it."
            )
        if not _admission_number_used(number):
            break
    counter.save(update_fields=["last"])
    return number


def preview_next_admission_number(year: int) -> str:
    """Sample of the next number. Writes nothing: neither counter nor settings row."""
    conf = academic_settings_or_defaults()
    counter = AdmissionCounter.objects.filter(year=year).first()
    return render_admission_number(
        conf.admission_pattern, conf.admission_prefix, year, (counter.last if counter else 0) + 1
    )


def update_admission_settings(*, prefix: str, pattern: str, by: Any, request: Any = None):
    prefix = (prefix or "").strip().upper()
    pattern = (pattern or "").strip().upper()
    validate_admission_prefix(prefix)
    validate_admission_pattern(pattern, prefix)
    with transaction.atomic():
        academic_write_lock()
        conf = get_academic_settings()
        before = {"prefix": conf.admission_prefix, "pattern": conf.admission_pattern}
        after = {"prefix": prefix, "pattern": pattern}
        if before != after:
            conf.admission_prefix, conf.admission_pattern = prefix, pattern
            conf.save(update_fields=["admission_prefix", "admission_pattern"])
            record(
                "academics.admission_pattern_changed",
                request=request,
                actor=by,
                obj=conf,
                before=before,
                after=after,
            )
    return conf
