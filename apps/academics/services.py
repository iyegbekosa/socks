import re
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import ProtectedError, RestrictedError
from django.utils.text import slugify

from apps.academics.models import (
    SECTIONS,
    AcademicSession,
    ClassArm,
    ClassLevel,
    Subject,
    Term,
)
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
        arm.suffix = (suffix or "").strip()
        arm.stream = stream or None
        arm.full_clean()
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
            f"{label} has enrolments, so it cannot be deleted. Move or withdraw the students first."
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
        subject.name = _check_subject_name(name, subject.pk)
        subject.sections = _clean_sections(sections)
        subject.full_clean()
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
