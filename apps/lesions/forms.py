from django import forms


class ProposalForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_fingerprint = forms.CharField(max_length=64, widget=forms.HiddenInput)
    expectations = forms.JSONField(widget=forms.HiddenInput)
    expected_lesion_revisions = forms.JSONField(required=False, widget=forms.HiddenInput)
    target = forms.ChoiceField(label="保存到哪个病灶标识")
    name = forms.CharField(label="新观察名称", max_length=120, required=False,
                          help_text="使用便于自己辨认的名称；这不会更改原报告的部位或结论。")
    checked_original = forms.BooleanField(required=False, label="我已查看两端原件，确认这些观察使用同一病灶标识")

    def __init__(self, *args, targets=(), action="CONFIRM", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].choices = targets or [("NEW", "建立新标识")]
        self.action = action
        if action in {"REJECT", "DEFER"}:
            self.fields["target"].required = False

    def clean(self):
        data = super().clean()
        if self.action not in {"CONFIRM", "REJECT", "DEFER"}:
            raise forms.ValidationError("请选择确认、拒绝或暂缓。")
        if self.action == "CONFIRM":
            if not data.get("checked_original"):
                self.add_error("checked_original", "请先查看两端原件，再明确确认关联。")
            if data.get("target") == "NEW" and not data.get("name"):
                self.add_error("name", "请输入新观察名称。")
        return data

    def arguments(self):
        return {**{key: self.cleaned_data[key] for key in ("expected_revision", "expected_fingerprint", "expectations",
                    "expected_lesion_revisions", "name", "checked_original")},
                "target_lesion_id": None if self.cleaned_data["target"] in {"", "NEW"} else self.cleaned_data["target"]}


class CreateForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    name = forms.CharField(label="新观察名称", max_length=120)
    checked_original = forms.BooleanField(label="我已核对原件，选择为这条观察建立稳定标识")


class NameForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    name = forms.CharField(label="观察名称", max_length=120)


class SelectPairForm(forms.Form):
    first_id = forms.ChoiceField(label="第一条报告观察")
    second_id = forms.ChoiceField(label="第二条报告观察")

    def __init__(self, *args, observations=(), **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(row["id"], row["label"]) for row in observations if row["status"] != "UNAVAILABLE"]
        for key in ("first_id", "second_id"):
            self.fields[key].choices = [("", "请选择原文观察"), *choices]

    def clean(self):
        data = super().clean()
        if data.get("first_id") and data.get("first_id") == data.get("second_id"):
            raise forms.ValidationError("请选择两条不同的原文观察。")
        return data


class MatchForm(ProposalForm):
    first_id = forms.UUIDField(widget=forms.HiddenInput)
    second_id = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["expected_revision"]
        del self.fields["expected_fingerprint"]

    def arguments(self):
        return {**{key: self.cleaned_data[key] for key in ("first_id", "second_id", "expectations",
                    "expected_lesion_revisions", "name", "checked_original")},
                "target_lesion_id": None if self.cleaned_data["target"] == "NEW" else self.cleaned_data["target"]}


class ManageForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expectations = forms.JSONField(widget=forms.HiddenInput)
    selected = forms.MultipleChoiceField(label="选择要调整的观察", widget=forms.CheckboxSelectMultiple)
    name = forms.CharField(label="拆分后的新名称", max_length=120, required=False)
    checked_original = forms.BooleanField(required=False, label="我已查看原件，确认所选观察应使用独立标识")

    def __init__(self, *args, observations=(), action="SPLIT", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected"].choices = [(row["id"], row["label"] + " · " + row["status_label"]) for row in observations]
        self.action = action

    def clean(self):
        data = super().clean()
        if self.action not in {"SPLIT", "UNLINK"}:
            raise forms.ValidationError("请选择拆分或取消关联。")
        if self.action == "SPLIT":
            if not data.get("name"):
                self.add_error("name", "拆分需要为独立观察填写名称。")
            if not data.get("checked_original"):
                self.add_error("checked_original", "请先核对原件，再确认拆分。")
        versions = data.get("expectations")
        selected = data.get("selected", [])
        if not isinstance(versions, dict) or any(identity not in versions for identity in selected):
            self.add_error("expectations", "观察核对版本不完整，请刷新后重试。")
        return data

    def arguments(self):
        data = self.cleaned_data
        return {"expected_revision": data["expected_revision"], "observation_ids": data["selected"],
                "expectations": {identity: data["expectations"][identity] for identity in data["selected"]}}
