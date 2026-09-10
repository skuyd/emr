"""Original-value controls for pathology; identity changes use group replacement."""
from django import forms

from .clinical_schema import FIELDS, validate_value
from .pathology_schema import ASSERTIONS


ASSERTION_LABELS = {
    "SOURCE_TEXT_ONLY_NOT_DIAGNOSED": "只保留原文，不作诊断",
    "AS_REPORTED_NO_POSITIVITY_INFERRED": "只保留原评分，不推算阳性",
    "POSITIVE": "原文明示阳性", "NEGATIVE": "原文明示阴性",
    "DETECTED": "原文明示检出", "NOT_DETECTED": "原文明示未检出",
    "UNCERTAIN": "原文明示不确定或可疑", "NOT_TESTED": "原文明示未检测",
    "NOT_PROVIDED": "原文未提供",
}
ROLE_LABELS = {
    "CURRENT_RESULT": "本次报告结果", "PRIMARY_ASSAY_METADATA": "本次标本或检测资料",
    "SUBMITTED_HISTORY": "送检病史", "HISTORICAL_QUOTE": "引用的既往结果",
    "EXPLANATION": "解释或参考说明", "CONTROL": "对照", "QC": "质控", "UNKNOWN": "角色未判断",
}


class PathologyValueForm(forms.Form):
    raw_value = forms.CharField(label="原件字段及关联依据文字", max_length=30000, widget=forms.Textarea(attrs={"rows": 3}))
    checked_original = forms.BooleanField(label="我已对照原件核对字段、标本、检测和原文限定", required=False)

    def __init__(self, field_key, *args, value=None, **kwargs):
        self.field_key, self.spec = field_key, FIELDS[field_key]
        if self.spec.category != "PATHOLOGY":
            raise ValueError("PathologyValueForm requires a pathology field")
        initial = dict(kwargs.pop("initial", {}) or {})
        value = value or {}
        kind = self.spec.value_type
        if kind in {"TEXT", "ASSERTED_TEXT"}:
            initial["text_value"] = value.get("text", "")
        elif kind == "DATE":
            initial.update(date_value=value.get("value") or "", precision=value.get("precision", "UNKNOWN"))
        elif kind == "CODED":
            initial.update(code=value.get("code", "UNKNOWN"), coded_raw=value.get("raw", ""))
        elif kind in {"IDENTITY", "MARKER"}:
            initial.update(identity_label=value.get("label", ""), identity_raw=value.get("raw", ""), marker_code=value.get("code") or "")
        elif kind == "IHC_SCORE":
            numbers = value.get("values", [])
            initial.update(score_kind=value.get("score_kind", ""), scalar_1=numbers[0] if numbers else "",
                           scalar_2=numbers[1] if len(numbers) == 2 else "", comparator=value.get("comparator", "EQ"),
                           original_unit=value.get("unit") or "")
        elif kind == "PATHOLOGY_DIMENSIONS":
            initial.update(measurement_object=value.get("measurement_object", "UNKNOWN"), measurement_role=value.get("measurement_role", "UNKNOWN"))
            for i, component in enumerate(value.get("components", []), 1):
                initial.update({f"size_{i}": component["value"], f"unit_{i}": component["unit"] or "", f"axis_{i}": component["axis"] or ""})
        elif kind == "NODE_COUNTS":
            initial["node_count"] = max(len(value.get("groups", [])), 1)
            for i, group in enumerate(value.get("groups", []), 1):
                initial.update({f"group_{i}_{key}": item if item is not None else "" for key, item in group.items()})
        initial.update(approximate=value.get("approximate", False), value_raw=value.get("raw", ""),
                       assertion=value.get("assertion", "AS_REPORTED_NO_POSITIVITY_INFERRED" if kind == "IHC_SCORE" else "SOURCE_TEXT_ONLY_NOT_DIAGNOSED"))
        super().__init__(*args, initial=initial, **kwargs)
        if kind in {"TEXT", "ASSERTED_TEXT"}:
            self.fields["text_value"] = forms.CharField(label=self.spec.label, max_length=30000, widget=forms.Textarea(attrs={"rows": 3}))
        elif kind == "DATE":
            self.fields["date_value"] = forms.CharField(label=self.spec.label, required=False, max_length=10, help_text="只填写本角色的原文日期；例如2030、2030-06、2030-06-18。未注明时留空。")
            self.fields["precision"] = forms.ChoiceField(label="原文日期精度", choices=[("UNKNOWN", "未注明"), ("YEAR", "年"), ("MONTH", "月"), ("DAY", "日")])
        elif kind == "CODED":
            labels = {"IHC": "免疫组化（IHC）", "ISH": "原位杂交（ISH）", "OTHER": "其他原文方法", "UNKNOWN": "方法未判断"}
            self.fields["code"] = forms.ChoiceField(label=self.spec.label, choices=[(code, labels[code]) for code in self.spec.codes])
            self.fields["coded_raw"] = forms.CharField(label="方法原词", max_length=512)
        elif kind in {"IDENTITY", "MARKER"}:
            self.fields["identity_label"] = forms.CharField(label=self.spec.label, max_length=80 if kind == "MARKER" else 256)
            self.fields["identity_raw"] = forms.CharField(label="身份原词", max_length=1000)
            if kind == "MARKER":
                self.fields["marker_code"] = forms.CharField(label="标记名称代码（未知留空）", required=False, max_length=40,
                                                              help_text="不确定名称时可留空；不能根据评分猜测标记。更换标记须整体替换关联。")
        elif kind == "IHC_SCORE":
            self.fields["score_kind"] = forms.ChoiceField(label="原文明示评分类型", choices=[("", "请选择原文类型"), ("TPS", "TPS 比例"), ("CPS", "CPS 分数"), ("IC", "IC 比例")])
            self.fields["scalar_1"] = forms.CharField(label="原文数值或范围下限", max_length=30)
            self.fields["scalar_2"] = forms.CharField(label="原文范围上限（单值留空）", max_length=30, required=False)
            self.fields["comparator"] = forms.ChoiceField(label="原文数值关系", choices=[("EQ", "单值"), ("LT", "小于"), ("LE", "小于或等于"), ("GT", "大于"), ("GE", "大于或等于"), ("RANGE", "范围")])
            self.fields["original_unit"] = forms.CharField(label="原单位（未印刷留空）", max_length=30, required=False,
                                                            help_text="不自动补百分号，不把 CPS 转为比例。")
        elif kind == "PATHOLOGY_DIMENSIONS":
            self.fields["measurement_object"] = forms.ChoiceField(label="原文明示测量对象", choices=[("UNKNOWN", "未注明是标本还是肿瘤"), ("SPECIMEN", "整体标本"), ("TUMOR", "肿瘤")])
            self.fields["measurement_role"] = forms.ChoiceField(label="原文时间角色", choices=[("UNKNOWN", "未注明"), ("CURRENT", "本次"), ("HISTORICAL", "引用既往")])
            axes = [("", "未注明"), ("LONG", "长径"), ("SHORT", "短径"), ("DIAMETER", "直径"), ("WIDTH", "宽"),
                    ("HEIGHT", "高"), ("DEPTH", "厚/深"), ("AP", "前后径"), ("TRANSVERSE", "横径"), ("CRANIOCAUDAL", "头尾径")]
            for i in range(1, 4):
                self.fields[f"size_{i}"] = forms.CharField(label=f"第{i}个原文尺寸", required=i == 1, max_length=30)
                self.fields[f"unit_{i}"] = forms.ChoiceField(label=f"第{i}个尺寸原单位", choices=[("", "原单位未注明"), ("mm", "mm"), ("cm", "cm"), ("毫米", "毫米"), ("厘米", "厘米")], required=False)
                self.fields[f"axis_{i}"] = forms.ChoiceField(label=f"第{i}个原文轴", choices=axes, required=False)
        elif kind == "NODE_COUNTS":
            self.fields["node_count"] = forms.IntegerField(label="原文分组数", min_value=1, max_value=100, widget=forms.HiddenInput)
            try:
                count = int(self.data.get(self.add_prefix("node_count"), 1) if self.is_bound else initial["node_count"])
            except (ValueError, TypeError):
                count = 1
            self.node_count = min(max(count, 1), 100)
            for i in range(1, self.node_count + 1):
                for key, label in (("label", "组名"), ("sampled", "送检数（未注明留空）"), ("positive", "原文明示阳性数（未注明留空）"), ("raw", "该组原文")):
                    self.fields[f"group_{i}_{key}"] = forms.CharField(label=f"第{i}组{label}", required=key in {"label", "raw"}, max_length=1000 if key == "raw" else 256)
        if kind in {"ASSERTED_TEXT", "IHC_SCORE", "NODE_COUNTS"}:
            allowed = {"AS_REPORTED_NO_POSITIVITY_INFERRED", "POSITIVE", "NEGATIVE", "UNCERTAIN"} if kind == "IHC_SCORE" else ASSERTIONS
            self.fields["assertion"] = forms.ChoiceField(label="原文明示状态", choices=[(code, label) for code, label in ASSERTION_LABELS.items() if code in allowed])
        if kind in {"IHC_SCORE", "PATHOLOGY_DIMENSIONS"}:
            self.fields["approximate"] = forms.BooleanField(label="原文有“约”等近似限定", required=False)
        if kind in {"IHC_SCORE", "PATHOLOGY_DIMENSIONS", "NODE_COUNTS"}:
            self.fields["value_raw"] = forms.CharField(label="本项数值原文（独立于完整关联依据）", required=False, max_length=30000,
                                                       help_text="保留原文的评分名称、单位、比较符号及否定；未填写时使用上方原件字段文字。")

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        kind = self.spec.value_type
        if kind in {"TEXT", "ASSERTED_TEXT"}:
            value = {"text": data["text_value"]}
            if kind == "ASSERTED_TEXT":
                value["assertion"] = data["assertion"]
        elif kind == "DATE":
            value = {"value": data["date_value"] or None, "precision": data["precision"]}
        elif kind == "CODED":
            value = {"code": data["code"], "raw": data["coded_raw"]}
        elif kind in {"IDENTITY", "MARKER"}:
            value = {"label": data["identity_label"], "raw": data["identity_raw"]}
            if kind == "MARKER":
                value["code"] = data["marker_code"] or None
        elif kind == "IHC_SCORE":
            value = {"score_kind": data["score_kind"], "values": [data["scalar_1"]] + ([data["scalar_2"]] if data["scalar_2"] else []),
                     "comparator": data["comparator"], "unit": data["original_unit"] or None,
                     "unit_state": "PRINTED" if data["original_unit"] else "NOT_PRINTED",
                     "scale_kind": "SCORE" if data["score_kind"] == "CPS" else "PROPORTION", "approximate": data["approximate"],
                     "assertion": data["assertion"], "raw": data["value_raw"] or data["raw_value"]}
        elif kind == "PATHOLOGY_DIMENSIONS":
            if not data.get("size_2") and data.get("size_3"):
                raise forms.ValidationError("请按原文顺序连续填写尺寸。")
            value = {"components": [{"value": data[f"size_{i}"], "unit": data[f"unit_{i}"] or None, "axis": data[f"axis_{i}"] or None}
                                    for i in range(1, 4) if data.get(f"size_{i}")], "approximate": data["approximate"],
                     "measurement_role": data["measurement_role"], "measurement_object": data["measurement_object"],
                     "raw": data["value_raw"] or data["raw_value"]}
        else:
            value = {"groups": [{key: data[f"group_{i}_{key}"] or None for key in ("label", "sampled", "positive", "raw")}
                                for i in range(1, data["node_count"] + 1)], "assertion": data["assertion"],
                     "raw": data["value_raw"] or data["raw_value"]}
        validate_value(self.field_key, value)
        data["value"] = value
        return data


class PathologyRevisionForm(PathologyValueForm):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(max_length=64, widget=forms.HiddenInput)


class PathologySourceForm(PathologyValueForm):
    """Explicit manual page transcription for the value and each bound identity."""
    page_number = forms.IntegerField(label="字段原文所在页", min_value=1)
    expected_report_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    source_role = forms.ChoiceField(label="这段原文的角色", choices=list(ROLE_LABELS.items()))

    def __init__(self, field_key, report, *args, anchors=(), bindings=(), fragments=(), **kwargs):
        from .pathology_schema import TARGET_KEYS

        self.report = report
        self.targets = {str(anchor.pk): anchor for anchor in anchors}
        initial = dict(kwargs.pop("initial", {}) or {})
        bound = {binding["role"]: binding for binding in bindings}
        for role in FIELDS[field_key].roles:
            key = role.lower()
            binding = bound.get(role)
            initial[f"binding_{key}"] = binding["target_fact_id"] if binding and binding["state"] == "BOUND" else "unknown:" + (binding["reason"] if binding else "NOT_STATED")
            proofs = [fragments[i] for i in binding["proof_fragment_ordinals"] if i < len(fragments)] if binding else []
            # Multi-page evidence is not silently attributed to the first page.
            same_page = len({piece["page"] for piece in proofs}) == 1
            initial[f"proof_{key}"] = "\n".join(piece["raw_text"] for piece in proofs) if same_page else ""
            initial[f"proof_page_{key}"] = proofs[0]["page"] if same_page else initial.get("page_number", "")
        super().__init__(field_key, *args, initial=initial, **kwargs)
        labels = {"SPECIMEN": "标本", "ASSAY": "检测", "MARKER": "标记"}
        unknown = [("unknown:NOT_STATED", "原文未提供，暂未关联"), ("unknown:AMBIGUOUS", "原文有多个可能归属，暂未关联"),
                   ("unknown:SOURCE_INCOMPLETE", "来源不完整，暂未关联"), ("unknown:UNKNOWN", "尚未判断归属")]
        for role in self.spec.roles:
            key = role.lower()
            choices = [*unknown, *[(str(anchor.pk), f"{anchor.automatic_content['value']['label']} · 第{anchor.document_page.page_number}页")
                                    for anchor in anchors if anchor.field_key == TARGET_KEYS[role]]]
            self.fields[f"binding_{key}"] = forms.ChoiceField(label=f"本字段关联的{labels[role]}", choices=choices,
                                                              help_text="只选择原文有明确关联的本报告身份；不能依靠位置或数值猜测。")
            self.fields[f"proof_{key}"] = forms.CharField(label=f"证明该{labels[role]}关联的原件文字", required=False, max_length=30000,
                                                          widget=forms.Textarea(attrs={"rows": 2}))
            self.fields[f"proof_page_{key}"] = forms.IntegerField(label=f"该{labels[role]}关联依据所在页", required=False, min_value=1)

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        fragments = [{"page_number": data["page_number"], "raw_text": data["raw_value"]}]
        bindings = []
        for role in self.spec.roles:
            key, identity = role.lower(), data[f"binding_{role.lower()}"]
            if identity.startswith("unknown:"):
                binding = {"role": role, "state": "UNKNOWN", "target_fact_id": None, "target_entity_key": None,
                           "proof_fragment_ordinals": [], "reason": identity.split(":", 1)[1]}
            else:
                if not data[f"proof_{key}"] or not data[f"proof_page_{key}"]:
                    self.add_error(f"proof_{key}", "绑定身份须填写能证明关联的原件文字和页码；没有依据时请选择暂未关联。")
                    continue
                target = self.targets[identity]
                binding = {"role": role, "state": "BOUND", "target_fact_id": identity, "target_entity_key": target.entity_key,
                           "proof_fragment_ordinals": [len(fragments)], "reason": None}
                fragments.append({"page_number": data[f"proof_page_{key}"], "raw_text": data[f"proof_{key}"]})
            bindings.append(binding)
        data.update(fragments=fragments, entity_context={"context_version": "IHC_CONTEXT_V1", "membership_policy": "IHC_CONTEXT_V1",
                                                        "report_id": str(self.report.pk), "bindings": bindings})
        return data


class ContextActionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    expected_material = forms.CharField(max_length=64, widget=forms.HiddenInput)
    checked_original = forms.BooleanField(label="我已逐项核对整组的原件文字、标本和检测关联", required=False)
