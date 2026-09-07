from django import forms

from apps.accounts.phone import InvalidPhone, normalize_mainland_phone
from .models import PatientMembership


class InvitationForm(forms.Form):
    recipient_phone = forms.CharField(label="接收者已验证手机号", max_length=32, widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"}))
    role = forms.ChoiceField(label="访问角色", choices=PatientMembership.Role.choices, initial="VIEWER")
    label = forms.CharField(label="成员称呼（可选）", required=False, max_length=80)

    def clean_recipient_phone(self):
        try:
            return normalize_mainland_phone(self.cleaned_data["recipient_phone"])
        except InvalidPhone:
            raise forms.ValidationError("请输入有效的中国大陆手机号。") from None
