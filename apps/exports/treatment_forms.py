"""The same explicit derived selectors for private exports and scoped sharing."""
from django import forms

from .treatment import SELECTION_KEYS


def add_derived_fields(form, patient, *, actor=None):
    from apps.treatments.workspace import trusted_workspace_material, workspace_material
    from apps.treatments.overlays import METRICS

    material = workspace_material(patient, actor=actor) if actor is not None else trusted_workspace_material(patient)
    form.treatment_material = material
    labels = {"treatment_event_ids": "选定治疗事件（含本人补记）", "regimen_ids": "选定方案及其中当前周期",
              "cycle_ids": "选定单个周期", "personal_change_ids": "选定个人变化"}
    for key in SELECTION_KEYS:
        form.fields[key] = forms.MultipleChoiceField(label=labels[key], required=False, widget=forms.CheckboxSelectMultiple)
    for key, rows, field in [("treatment_event_ids", material["events"], "title"), ("regimen_ids", material["regimens"], "text"),
                             ("cycle_ids", material["cycles"], "anchor")]:
        form.fields[key].choices = [(row["id"], f'{row["content"].get(field) or "日期不明"} · {"已确认" if row["status"] == "CONFIRMED" else "候选，尚待确认"} · 修订 {row["revision_number"]}')
                                    for row in rows if row["source_valid"] and row["status"] in {"CONFIRMED", "PENDING"}]
    form.fields["personal_change_ids"].choices = [(row["id"], f'{row["label"]} · {row["date"] or "日期不明"} · {row["numeric_value"] or "不可计算"} {row["unit"]}')
                                                 for row in material["records"] if row["kind"] == "observation" and row["source_valid"]]
    form.fields["cycle_metric_codes"] = forms.MultipleChoiceField(label="周期指标", choices=[(key, key) for key in METRICS],
        required=False, initial=list(METRICS), widget=forms.CheckboxSelectMultiple)
    form.fields["cycle_mode"] = forms.ChoiceField(label="周期明细", choices=[("key", "关键节点"), ("full", "完整明细")], required=False, initial="key")
    form.fields["include_pending_cycles"] = forms.BooleanField(label="明确纳入候选内容（仍标为尚待确认，PDF 仅放候选附页）", required=False)


def derived_selection(data):
    return {**{key: data[key] for key in SELECTION_KEYS}, "cycle_mode": data["cycle_mode"] or "key",
            "cycle_metric_codes": data["cycle_metric_codes"] or ["ANC", "PLT", "HGB"],
            "include_pending_cycles": data["include_pending_cycles"]}
