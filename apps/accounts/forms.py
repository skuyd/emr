from django import forms


class PasswordLoginForm(forms.Form):
    phone = forms.CharField(
        label="手机号",
        max_length=32,
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "numeric"}),
    )
    password = forms.CharField(
        label="密码",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class MfaForm(forms.Form):
    code = forms.RegexField(
        label="验证码",
        regex=r"^[0-9]{6}$",
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "pattern": "[0-9]*"}),
    )
