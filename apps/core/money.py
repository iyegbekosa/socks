"""Money is integer kobo everywhere; naira strings exist only at the edges."""

from decimal import Decimal, InvalidOperation

MAX_KOBO = 10_000_000_000  # ₦100,000,000.00


def kobo_from_naira(value: str | int | Decimal) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("Money must not be a float; pass a str, int or Decimal.")
    if not isinstance(value, str | int | Decimal):
        raise TypeError(f"Unsupported money type: {type(value).__name__}")
    try:
        naira = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid naira amount: {value!r}") from exc
    if not naira.is_finite():
        raise ValueError(f"Invalid naira amount: {value!r}")
    kobo = naira * 100
    if kobo != kobo.to_integral_value():
        raise ValueError("Naira amounts cannot have more than 2 decimal places.")
    if kobo < 0:
        raise ValueError("Amount cannot be negative.")
    if kobo > MAX_KOBO:
        raise ValueError("Amount is too large.")
    return int(kobo)


def naira_display(kobo: int) -> str:
    sign = "-" if kobo < 0 else ""
    whole, fraction = divmod(abs(kobo), 100)
    return f"{sign}₦{whole:,}.{fraction:02d}"


def money_fields(kobo: int) -> dict[str, int | str]:
    return {"amount_kobo": kobo, "amount_display": naira_display(kobo)}
