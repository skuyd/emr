from django import forms


class PhoneForm(forms.Form):
    phone = forms.CharField(label="手机号", max_length=32)
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class VerifyForm(PhoneForm):
    code = forms.CharField(label="验证码", min_length=6, max_length=6)
