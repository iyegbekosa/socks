"""Fee fixtures. Call inside ``schema_context("school_a")``."""

from datetime import date
from types import SimpleNamespace

from apps.academics.models import Term
from apps.accounts.roles import set_roles
from apps.fees.models import Discount, FeeItem, FeeStructure, FeeStructureLine
from tests.accounts.factories import StaffFactory
from tests.students.factories import default_arm, default_session


def make_terms() -> SimpleNamespace:
    """Session 2026/2027 with its 1st and 2nd terms, and the JSS 1 level."""
    session = default_session()
    term1, _ = Term.objects.get_or_create(
        session=session,
        number=1,
        defaults={"start_date": date(2026, 9, 1), "end_date": date(2026, 12, 15)},
    )
    term2, _ = Term.objects.get_or_create(
        session=session,
        number=2,
        defaults={"start_date": date(2027, 1, 5), "end_date": date(2027, 4, 1)},
    )
    return SimpleNamespace(session=session, term1=term1, term2=term2, level=default_arm().level)


def make_item(name: str = "Tuition", description: str = "") -> FeeItem:
    return FeeItem.objects.create(name=name, description=description)


def make_structure(level, term, lines: list[tuple[FeeItem, int, bool]]) -> FeeStructure:
    structure = FeeStructure.objects.create(level=level, term=term)
    for item, amount, optional in lines:
        FeeStructureLine.objects.create(
            structure=structure, item=item, amount_kobo=amount, is_optional=optional
        )
    return structure


def make_percent_discount(name: str = "Staff child", percent: int = 10) -> Discount:
    return Discount.objects.create(name=name, kind=Discount.PERCENT, percent=percent)


def make_fixed_discount(name: str = "Scholarship", amount_kobo: int = 500_000) -> Discount:
    return Discount.objects.create(name=name, kind=Discount.FIXED, amount_kobo=amount_kobo)


def make_staff(role: str):
    user = StaffFactory()
    set_roles(user, [role], by=user)
    return user
