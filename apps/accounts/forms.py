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


class FirstUsePhoneForm(forms.Form):
    phone = forms.CharField(
        label="手机号",
        max_length=32,
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel", "inputmode": "numeric"}),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class SetPasswordForm(forms.Form):
    password = forms.CharField(
        label="密码",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password_confirm = forms.CharField(
        label="确认密码",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirmation = cleaned_data.get("password_confirm")
        if password is not None and confirmation is not None and password != confirmation:
            self.add_error("password_confirm", "两次输入的密码不一致。")
        return cleaned_data
