from django import forms
from django.forms import BaseFormSet, formset_factory


class ComponentForm(forms.Form):
    name = forms.CharField(max_length=40, strip=True)
    max_score = forms.IntegerField(min_value=1, max_value=100)
    order = forms.IntegerField(min_value=0, max_value=1000, initial=0)


class BandForm(forms.Form):
    lower = forms.IntegerField(min_value=0, max_value=100)
    upper = forms.IntegerField(min_value=0, max_value=100)
    grade = forms.CharField(max_length=4, strip=True)
    remark = forms.CharField(max_length=40, required=False, strip=True)


class _BaseRows(BaseFormSet):
    def kept(self) -> list[dict]:
        """Cleaned rows that are not deleted and not entirely blank."""
        rows = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE"):
                continue
            rows.append(data)
        return rows


ComponentFormSet = formset_factory(
    ComponentForm, formset=_BaseRows, extra=2, can_delete=True, max_num=20, validate_max=True
)
BandFormSet = formset_factory(
    BandForm, formset=_BaseRows, extra=2, can_delete=True, max_num=30, validate_max=True
)
