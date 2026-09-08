import uuid

from django import forms

from .validation import event_content, validate_day


PRECISIONS = [("UNKNOWN", "日期不明"), ("DAY", "精确到日 YYYY-MM-DD"), ("MONTH", "仅年月 YYYY-MM"), ("YEAR", "仅年份 YYYY")]
KINDS = [("SYSTEMIC_TREATMENT", "药物治疗"), ("CELL_THERAPY", "细胞治疗"), ("RADIOTHERAPY", "放疗"),
         ("SURGERY", "手术"), ("PROCEDURE", "其他操作"), ("LOCAL_PROCEDURE", "局部治疗"),
         ("ADMISSION", "入院"), ("DISCHARGE", "出院"), ("PAUSE", "暂停"), ("DELAY", "延迟"),
         ("STOP", "停止或完成"), ("ASSESSMENT", "明确评估节点"), ("MEDICATION_ORDER", "医嘱记录")]
OCCURRENCES = [("OCCURRED", "已发生（来源记载或本人补记）"), ("PLANNED", "计划或建议"),
               ("NEGATED", "明确未发生"), ("UNKNOWN", "发生状态不明"), ("ORDER", "仅医嘱，执行待核对")]


class OperationForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)


class DecisionForm(OperationForm):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_sources = forms.JSONField(required=False, widget=forms.HiddenInput)
    checked_original = forms.BooleanField(label="我已核对原件或本人记录", required=False)


class EventForm(DecisionForm):
    title = forms.CharField(label="事件名称", max_length=200)
    kind = forms.ChoiceField(label="事件类别", choices=KINDS)
    occurrence = forms.ChoiceField(label="发生状态", choices=OCCURRENCES)
    date = forms.CharField(label="事件日期", max_length=10, required=False)
    date_precision = forms.ChoiceField(label="日期精度", choices=PRECISIONS)
    regimen_text = forms.CharField(label="原文或本人记录的方案", max_length=1000, required=False)
    cycle_ordinal = forms.IntegerField(label="原文或本人确认的周期序号（未知留空）", min_value=1, max_value=9999, required=False)
    cycle_day = forms.IntegerField(label="明确周期第几天（未知留空）", min_value=1, max_value=9999, required=False)
    note = forms.CharField(label="说明", max_length=5000, required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def clean(self):
        values = super().clean()
        if not self.errors:
            event_content({key: values.get(key) for key in ["title", "kind", "occurrence", "date", "date_precision", "regimen_text", "cycle_ordinal", "cycle_day", "note"]})
        return values


class RegimenForm(DecisionForm):
    text = forms.CharField(label="方案名称或原文", max_length=1000)
    note = forms.CharField(label="说明", max_length=5000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    event_ids = forms.MultipleChoiceField(label="方案依据（本人独立补记可不选资料）", required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, events=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event_ids"].choices = [(row["id"], row["content"]["title"]) for row in events]


class CycleForm(DecisionForm):
    anchor = forms.CharField(label="已核对的周期锚点", max_length=10, required=False)
    anchor_precision = forms.ChoiceField(label="锚点日期精度", choices=PRECISIONS)
    ordinal = forms.IntegerField(label="原文或本人确认的周期序号（未知留空）", min_value=1, max_value=9999, required=False)
    end = forms.CharField(label="明确记载的实际结束日期（未知留空）", max_length=10, required=False)
    end_precision = forms.ChoiceField(label="结束日期精度", choices=PRECISIONS)
    regimen_id = forms.ChoiceField(label="所属方案", required=False)
    note = forms.CharField(label="周期说明", max_length=5000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    event_ids = forms.MultipleChoiceField(label="核对本周期的治疗或住院依据", required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, events=(), regimens=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event_ids"].choices = [(row["id"], f'{row["content"]["title"]} · {row["content"].get("date") or "日期不明"}') for row in events]
        self.fields["regimen_id"].choices = [("", "方案不明或暂不归组"), *[(row["id"], row["content"]["text"]) for row in regimens]]

    def clean(self):
        values = super().clean()
        if not self.errors:
            for field, precision in [("anchor", "anchor_precision"), ("end", "end_precision")]:
                if field in values:
                    values[field] = validate_day(values[field], values[precision])
            if values.get("anchor_precision") == values.get("end_precision") == "DAY" and values["end"] < values["anchor"]:
                raise forms.ValidationError("结束日期不能早于锚点。")
        return values


class ProposalForm(OperationForm):
    expected_fingerprint = forms.RegexField(r"^[a-f0-9]{64}$", widget=forms.HiddenInput)
    proposal_id = forms.RegexField(r"^[a-f0-9]{64}$", required=False, widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=0, required=False, widget=forms.HiddenInput)
    checked_original = forms.BooleanField(label="我已核对提议所列的原件或本人记录", required=False)


class MergeForm(CycleForm):
    cycle_ids = forms.MultipleChoiceField(label="需要合并的周期", widget=forms.CheckboxSelectMultiple)
    expected_revisions = forms.JSONField(widget=forms.HiddenInput)

    def __init__(self, *args, cycles=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cycle_ids"].choices = [(row["id"], f'{row["content"].get("anchor") or "锚点不明"} · {row["content"].get("ordinal") or "序号不明"}') for row in cycles]
        self.fields.pop("event_ids")


class SplitPartForm(CycleForm):
    record_link_ids = forms.MultipleChoiceField(label="分配到此部分的检查（可留未分组）", required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, record_links=(), **kwargs):
        super().__init__(*args, **kwargs)
        for key in ["operation_id", "expected_revision", "expected_sources", "checked_original"]:
            self.fields.pop(key)
        self.fields["event_ids"].required = True
        self.fields["record_link_ids"].choices = [(row["id"], row.get("label", row["source_id"])) for row in record_links]


class AssignmentForm(DecisionForm):
    record_key = forms.ChoiceField(label="资料、报告或检验")
    expected_record_sources = forms.JSONField(widget=forms.HiddenInput)
    expected_assignments = forms.JSONField(widget=forms.HiddenInput)
    assigned = forms.ChoiceField(label="归属决定", choices=[("true", "关联到此周期"), ("false", "明确保留未分组")])

    def __init__(self, *args, records=(), **kwargs):
        super().__init__(*args, **kwargs)
        kinds = {"document": "整份资料", "report": "报告", "observation": "检验"}
        self.fields["record_key"].choices = [(f'{row["kind"]}:{row["id"]}', f'{kinds[row["kind"]]} · {row["label"]} · {row.get("date") or "日期不明"}') for row in records]
