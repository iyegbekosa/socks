from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.academics.models import Term
from apps.accounts.roles import SCHOOL_ADMIN
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.fees.models import (
    Discount,
    FeeItem,
    FeeSettings,
    FeeStructure,
    FeeStructureLine,
    StudentDiscount,
)

THRESHOLD_FIELD = "manual_approval_threshold_kobo"


def _lines_snapshot(structure: FeeStructure) -> list[dict[str, Any]]:
    return [
        {"item_id": line.item_id, "amount_kobo": line.amount_kobo, "optional": line.is_optional}
        for line in structure.lines.all().order_by("item_id")
    ]


def save_item(item: FeeItem, *, by: Any, request: Any = None) -> FeeItem:
    with transaction.atomic():
        created = item.pk is None
        item.save()
        record(
            "fees.item_saved",
            request=request,
            actor=by,
            obj=item,
            after={"created": created},
        )
    return item


def save_discount(discount: Discount, *, by: Any, request: Any = None) -> Discount:
    with transaction.atomic():
        created = discount.pk is None
        discount.save()
        record(
            "fees.discount_saved",
            request=request,
            actor=by,
            obj=discount,
            after={
                "created": created,
                "kind": discount.kind,
                "percent": discount.percent,
                "amount_kobo": discount.amount_kobo,
            },
        )
    return discount


def save_settings(values: dict[str, Any], *, by: Any, request: Any = None) -> FeeSettings:
    """``values`` maps FeeSettings field names to new values.

    Only a school admin may change ``manual_approval_threshold_kobo`` (Ruling H): otherwise
    a bursar could raise it, record a large payment that confirms at once, and lower it.
    The audit lists only the fields that changed (the note as ``note_changed``, not its text),
    and is skipped when nothing changed.
    """
    if THRESHOLD_FIELD in values and not (by is not None and by.has_role(SCHOOL_ADMIN)):
        raise PermissionDenied("Only a school admin can change the approval threshold.")
    with transaction.atomic():
        school_lock("fees:settings")
        conf = FeeSettings.load()
        changed = [f for f, v in values.items() if getattr(conf, f) != v]
        if not changed:
            return conf
        numeric = [f for f in changed if f != "bank_transfer_note"]
        before = {f: getattr(conf, f) for f in numeric}
        for field, value in values.items():
            setattr(conf, field, value)
        conf.save()
        after: dict[str, Any] = {f: getattr(conf, f) for f in numeric}
        if "bank_transfer_note" in changed:
            after["note_changed"] = True
        record(
            "fees.settings_saved",
            request=request,
            actor=by,
            obj=conf,
            before=before,
            after=after,
        )
    return conf


def save_structure(
    level: Any,
    term: Term,
    lines: dict[int, tuple[int, bool]],
    *,
    by: Any,
    request: Any = None,
) -> FeeStructure:
    """Make the structure for (level, term) hold exactly ``lines`` (item id -> (kobo, optional))."""
    with transaction.atomic():
        school_lock(f"fees:structure:{level.pk}:{term.pk}")
        structure, created = FeeStructure.objects.get_or_create(level=level, term=term)
        before = None if created else _lines_snapshot(structure)
        existing = {line.item_id: line for line in structure.lines.all()}
        structure.lines.exclude(item_id__in=lines).delete()
        for item_id, (amount, optional) in lines.items():
            line = existing.get(item_id)
            if line is None:
                FeeStructureLine.objects.create(
                    structure=structure, item_id=item_id, amount_kobo=amount, is_optional=optional
                )
            elif (line.amount_kobo, line.is_optional) != (amount, optional):
                line.amount_kobo, line.is_optional = amount, optional
                line.save(update_fields=["amount_kobo", "is_optional"])
        record(
            "fees.structure_saved",
            request=request,
            actor=by,
            obj=structure,
            before={"lines": before} if before is not None else None,
            after={
                "level_id": level.pk,
                "term_id": term.pk,
                "lines": _lines_snapshot(structure),
            },
        )
    return structure


def copy_structures(from_term: Term, to_term: Term, by: Any, *, request: Any = None) -> int:
    """Copy every structure of ``from_term`` to ``to_term``, skipping levels that have one."""
    if from_term.pk == to_term.pk:
        raise ValidationError("Choose two different terms.")
    copied = 0
    for source in FeeStructure.objects.filter(term=from_term).select_related("level"):
        with transaction.atomic():
            school_lock(f"fees:structure:{source.level_id}:{to_term.pk}")
            if FeeStructure.objects.filter(level_id=source.level_id, term=to_term).exists():
                continue
            target = FeeStructure.objects.create(level_id=source.level_id, term=to_term)
            FeeStructureLine.objects.bulk_create(
                FeeStructureLine(
                    structure=target,
                    item_id=line.item_id,
                    amount_kobo=line.amount_kobo,
                    is_optional=line.is_optional,
                )
                for line in source.lines.all()
            )
            record(
                "fees.structure_saved",
                request=request,
                actor=by,
                obj=target,
                after={
                    "level_id": target.level_id,
                    "term_id": to_term.pk,
                    "copied_from_term_id": from_term.pk,
                    "lines": _lines_snapshot(target),
                },
            )
            copied += 1
    return copied


def assign_discount(
    student: Any, discount: Discount, session: Any, *, by: Any, request: Any = None
):
    with transaction.atomic():
        row = StudentDiscount.objects.create(student=student, discount=discount, session=session)
        record(
            "fees.student_discount_set",
            request=request,
            actor=by,
            obj=row,
            after={
                "student_id": student.pk,
                "discount_id": discount.pk,
                "session_id": session.pk,
            },
        )
    return row


def remove_discount(row: StudentDiscount, *, by: Any, request: Any = None) -> None:
    with transaction.atomic():
        before = {
            "student_id": row.student_id,
            "discount_id": row.discount_id,
            "session_id": row.session_id,
        }
        record("fees.student_discount_set", request=request, actor=by, obj=row, before=before)
        row.delete()


def save_payout_account(
    gateway: Any,
    *,
    bank_code: str,
    bank_name: str,
    account_number: str,
    by: Any,
    request: Any = None,
) -> Any:
    """Create or update the current school's Paystack subaccount and keep its masked details.

    Runs under a per-school lock with the School row locked, so two admins confirming at once
    can't create two subaccounts. Raises ``GatewayUnavailable`` without changing anything.
    """
    from apps.tenancy.limits import _current_school
    from apps.tenancy.models import School

    current = _current_school()
    if current is None:
        raise PermissionDenied
    with transaction.atomic():
        school_lock("fees.payout_account")
        school = School.objects.select_for_update().get(pk=current.pk)
        existing = school.paystack_subaccount_code
        code = gateway.save_subaccount(
            code=existing or None,
            business_name=school.name,
            bank_code=bank_code,
            account_number=account_number,
        )
        school.paystack_subaccount_code = code
        school.payout_bank_name = bank_name
        school.payout_account_last4 = account_number[-4:]
        school.save(
            update_fields=["paystack_subaccount_code", "payout_bank_name", "payout_account_last4"]
        )
        record(
            "fees.subaccount_saved",
            request=request,
            actor=by,
            obj=school,
            after={
                "bank_code": bank_code,
                "account_last4": account_number[-4:],
                "updated": bool(existing),
            },
        )
    # Keep the request's tenant object (connection.tenant) in step with the saved row.
    for name in ("paystack_subaccount_code", "payout_bank_name", "payout_account_last4"):
        setattr(current, name, getattr(school, name))
    return school
