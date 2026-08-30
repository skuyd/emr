from django import forms

from .policies import REQUIRED_CONSENT_TYPES
from .services import normalize_display_name


CONSENT_LABELS = {
    "privacy": "我已阅读并同意隐私政策",
    "sensitive_data": "我已阅读并单独同意敏感个人信息处理规则",
    "upload_authority": "我确认有权上传并管理相关资料",
}


class OnboardingForm(forms.Form):
    display_name = forms.CharField(
        label="患者称呼",
        max_length=80,
        widget=forms.TextInput(attrs={"placeholder": "例如：妈妈、王女士、我自己", "autocomplete": "name"}),
    )
    privacy = forms.BooleanField(label=CONSENT_LABELS["privacy"], required=True)
    sensitive_data = forms.BooleanField(label=CONSENT_LABELS["sensitive_data"], required=True)
    upload_authority = forms.BooleanField(label=CONSENT_LABELS["upload_authority"], required=True)

    def clean_display_name(self):
        return normalize_display_name(self.cleaned_data["display_name"])


class ReconsentForm(forms.Form):
    def __init__(self, missing_types, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for consent_type in missing_types:
            if consent_type not in REQUIRED_CONSENT_TYPES:
                raise ValueError("Unknown consent type")
            self.fields[consent_type] = forms.BooleanField(label=CONSENT_LABELS[consent_type], required=True)
