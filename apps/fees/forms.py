from typing import Any

from django import forms

from apps.academics.models import AcademicSession, Term
from apps.core.money import kobo_from_naira
from apps.fees.models import Discount, FeeItem, FeeSettings, StudentDiscount

NAIRA_ATTRS = {"inputmode": "decimal", "autocomplete": "off"}


def parse_naira(raw: str, *, label: str = "Amount") -> int:
    """Naira text from a form to kobo, raising ValidationError with a friendly message."""
    text = (raw or "").strip().replace(",", "").lstrip("₦").strip()
    try:
        return kobo_from_naira(text)
    except (ValueError, TypeError) as exc:
        message = str(exc)
        if message.startswith("Invalid naira amount"):
            message = f"{label} must be a number in naira, for example 1500.00."
        raise forms.ValidationError(message) from exc


class NairaField(forms.CharField):
    """Text input for naira; ``clean`` returns integer kobo (or None when blank and optional)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", 20)
        kwargs.setdefault("widget", forms.TextInput(attrs=NAIRA_ATTRS))
        super().__init__(**kwargs)

    def clean(self, value: Any) -> int | None:
        text = super().clean(value)
        if not text:
            return None
        return parse_naira(text, label=str(self.label or "Amount"))


def _unique_name_error(model: Any, name: str, exclude_pk: int | None, what: str) -> str:
    qs = model.objects.filter(name__iexact=name)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise forms.ValidationError(f"A {what} with this name already exists.")
    return name


class FeeItemForm(forms.Form):
    name = forms.CharField(max_length=80)
    description = forms.CharField(max_length=200, required=False)

    def __init__(self, *args: Any, instance: FeeItem | None = None, **kwargs: Any) -> None:
        self.instance = instance
        if instance is not None:
            kwargs.setdefault(
                "initial", {"name": instance.name, "description": instance.description}
            )
        super().__init__(*args, **kwargs)

    def clean_name(self) -> str:
        name = " ".join(self.cleaned_data["name"].split())
        return _unique_name_error(
            FeeItem, name, self.instance.pk if self.instance else None, "fee item"
        )

    def build(self) -> FeeItem:
        item = self.instance or FeeItem()
        item.name = self.cleaned_data["name"]
        item.description = self.cleaned_data["description"].strip()
        return item


class DiscountForm(forms.Form):
    name = forms.CharField(max_length=80)
    kind = forms.ChoiceField(choices=Discount.KIND_CHOICES)
    percent = forms.IntegerField(
        required=False, min_value=1, max_value=100, help_text="Whole number from 1 to 100."
    )
    amount = NairaField(required=False, label="Amount (naira)")

    def __init__(self, *args: Any, instance: Discount | None = None, **kwargs: Any) -> None:
        self.instance = instance
        if instance is not None:
            kwargs.setdefault(
                "initial",
                {
                    "name": instance.name,
                    "kind": instance.kind,
                    "percent": instance.percent,
                    "amount": f"{instance.amount_kobo / 100:.2f}"
                    if instance.amount_kobo is not None
                    else "",
                },
            )
        super().__init__(*args, **kwargs)

    def clean_name(self) -> str:
        name = " ".join(self.cleaned_data["name"].split())
        return _unique_name_error(
            Discount, name, self.instance.pk if self.instance else None, "discount"
        )

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        kind = data.get("kind")
        if kind == Discount.PERCENT and not self.has_error("percent"):
            if data.get("percent") is None:
                self.add_error("percent", "Enter a percentage from 1 to 100.")
        elif kind == Discount.FIXED and not self.has_error("amount"):
            if not data.get("amount"):
                self.add_error("amount", "Enter an amount greater than zero.")
        return data

    def build(self) -> Discount:
        d = self.cleaned_data
        discount = self.instance or Discount()
        discount.name = d["name"]
        discount.kind = d["kind"]
        if d["kind"] == Discount.PERCENT:
            discount.percent, discount.amount_kobo = d["percent"], None
        else:
            discount.percent, discount.amount_kobo = None, d["amount"]
        return discount


class FeeSettingsForm(forms.Form):
    allow_part_payment = forms.BooleanField(required=False, label="Allow part payment")
    min_part_payment = NairaField(
        required=False,
        label="Minimum part payment (naira)",
        help_text="Leave blank or 0 for no minimum.",
    )
    manual_approval_threshold = NairaField(
        required=False,
        label="Manual payment approval threshold (naira)",
        help_text="Manual payments at or above this need a second person to approve.",
    )
    block_results_when_owing = forms.BooleanField(
        required=False, label="Withhold results from students who owe fees"
    )
    bank_transfer_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=500,
        label="Bank transfer note",
        help_text="Shown to students when online payment is unavailable.",
    )

    def __init__(self, *args: Any, conf: FeeSettings | None = None, **kwargs: Any) -> None:
        if conf is not None:
            kwargs.setdefault(
                "initial",
                {
                    "allow_part_payment": conf.allow_part_payment,
                    "min_part_payment": f"{conf.min_part_payment_kobo / 100:.2f}",
                    "manual_approval_threshold": f"{conf.manual_approval_threshold_kobo / 100:.2f}",
                    "block_results_when_owing": conf.block_results_when_owing,
                    "bank_transfer_note": conf.bank_transfer_note,
                },
            )
        super().__init__(*args, **kwargs)

    def values(self) -> dict[str, Any]:
        d = self.cleaned_data
        return {
            "allow_part_payment": d["allow_part_payment"],
            "min_part_payment_kobo": d["min_part_payment"] or 0,
            "manual_approval_threshold_kobo": d["manual_approval_threshold"] or 0,
            "block_results_when_owing": d["block_results_when_owing"],
            "bank_transfer_note": d["bank_transfer_note"].strip(),
        }


class StructureForm(forms.Form):
    """One row per fee item: an amount (blank means not in the structure) and an optional flag."""

    def __init__(self, items: list[FeeItem], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.items = items
        for item in items:
            self.fields[f"amount_{item.pk}"] = NairaField(required=False, label=item.name)
            self.fields[f"optional_{item.pk}"] = forms.BooleanField(
                required=False, label=f"{item.name} is optional"
            )

    def rows(self) -> list[tuple[FeeItem, Any, Any]]:
        return [
            (item, self[f"amount_{item.pk}"], self[f"optional_{item.pk}"]) for item in self.items
        ]

    def lines(self) -> dict[int, tuple[int, bool]]:
        out: dict[int, tuple[int, bool]] = {}
        for item in self.items:
            amount = self.cleaned_data.get(f"amount_{item.pk}")
            if amount is not None:
                out[item.pk] = (amount, bool(self.cleaned_data.get(f"optional_{item.pk}")))
        return out


class CopyStructuresForm(forms.Form):
    from_term = forms.ModelChoiceField(
        queryset=Term.objects.select_related("session"), label="Copy from"
    )
    to_term = forms.ModelChoiceField(
        queryset=Term.objects.select_related("session"), label="Copy to"
    )

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if data.get("from_term") and data.get("from_term") == data.get("to_term"):
            self.add_error("to_term", "Choose two different terms.")
        return data


class StudentDiscountForm(forms.Form):
    discount = forms.ModelChoiceField(queryset=Discount.objects.all())
    session = forms.ModelChoiceField(queryset=AcademicSession.objects.all())

    def __init__(self, *args: Any, student: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.student = student

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if data.get("discount") and data.get("session"):
            if StudentDiscount.objects.filter(
                student=self.student, discount=data["discount"], session=data["session"]
            ).exists():
                raise forms.ValidationError("This student already has that discount this session.")
        return data
