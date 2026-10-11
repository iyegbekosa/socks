from django import forms


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
