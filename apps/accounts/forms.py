from django import forms
from django.contrib.auth.forms import SetPasswordForm


class LoginForm(forms.Form):
    identifier = forms.CharField(
        label="Phone, email or admission number",
        max_length=254,
        widget=forms.TextInput(
            attrs={"autocomplete": "username", "autocapitalize": "none", "autofocus": True}
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class ResetRequestForm(forms.Form):
    identifier = forms.CharField(
        label="Phone, email or admission number",
        max_length=254,
        widget=forms.TextInput(
            attrs={"autocomplete": "username", "autocapitalize": "none", "autofocus": True}
        ),
    )


class ResetVerifyForm(SetPasswordForm):
    code = forms.CharField(
        label="6-digit code",
        max_length=10,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "autofocus": True}
        ),
    )
    field_order = ["code", "new_password1", "new_password2"]
