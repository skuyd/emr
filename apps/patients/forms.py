from django import forms

from .services import normalize_display_name


class OnboardingForm(forms.Form):
    display_name = forms.CharField(
        label="如何称呼您",
        max_length=80,
        widget=forms.TextInput(attrs={"placeholder": "例如：王小明", "autocomplete": "name"}),
    )
    privacy = forms.BooleanField(label="我已阅读并同意隐私政策", required=True)
    sensitive_data = forms.BooleanField(label="我已阅读敏感信息说明", required=True)
    upload_authority = forms.BooleanField(label="我同意授权上传本人健康资料", required=True)

    def clean_display_name(self):
        return normalize_display_name(self.cleaned_data["display_name"])
