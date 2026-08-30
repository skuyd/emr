from django import forms


class LoginForm(forms.Form):
    phone = forms.CharField(
        label="手机号",
        max_length=32,
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "numeric"}),
    )
    code = forms.CharField(
        label="验证码",
        required=False,
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "pattern": "[0-9]*"}),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class PhoneRequestForm(forms.Form):
    phone = forms.CharField(max_length=32)


class VerifyForm(forms.Form):
    phone = forms.CharField(max_length=32)
    code = forms.CharField(min_length=6, max_length=6)
