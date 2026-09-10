"""Original-value controls; no free-form JSON or inferred missing components."""
from django import forms

from .clinical_schema import FIELDS, validate_value
from .molecular_schema import SCHEMA


STATES = [("UNKNOWN", "尚未判断"), ("PRINTED", "原件明确印有"), ("NOT_PRINTED", "已核对原件确实未印")]
CODE_LABELS = {"MSI_H": "原文 MSI-H", "MSI_L": "原文 MSI-L", "MSS": "原文 MSS", "UNKNOWN": "尚未判断",
    "HIGH": "原文高", "INTERMEDIATE": "原文中", "LOW": "原文低", "REPORT_BENEFIT": "报告记载可能获益",
    "REPORT_RESISTANCE": "报告记载耐药", "REPORT_NO_BENEFIT": "报告记载未获益", "UNCERTAIN": "原文明确不确定",
    "NOT_STATED": "已核对报告未说明", "NEGATIVE": "原文阴性", "NOT_DETECTED": "原文未检出",
    "NOT_TESTED": "原文未检测", "NOT_PROVIDED": "原文注明未提供"}


def lines(text):
    # Keep order and repeated components. Blank lines are separators only.
    return [line.strip() for line in text.splitlines() if line.strip()]


class MolecularValueForm(forms.Form):
    raw_value = forms.CharField(label="本字段及关联依据的原件文字", max_length=30000, widget=forms.Textarea(attrs={"rows": 3}))
    checked_original = forms.BooleanField(label="我已逐项对照原件核对原值、范围和必要归属", required=False)

    def __init__(self, field_key, *args, value=None, **kwargs):
        self.field_key, self.spec = field_key, FIELDS[field_key]
        if self.spec.version != SCHEMA:
            raise ValueError("MolecularValueForm requires a molecular field")
        self.original_value = value or {}
        initial = dict(kwargs.pop("initial", {}) or {})
        super().__init__(*args, initial=initial, **kwargs)
        value = self.original_value
        kind = self.spec.value_type
        def text(key, label, actual="", *, required=True, maximum=30000, multiple=False):
            self.fields[key] = forms.CharField(label=label, required=required, max_length=maximum,
                widget=forms.Textarea(attrs={"rows": 3 if multiple else 2}))
            self.initial.setdefault(key, actual or "")
        def choice(key, label, options, actual):
            self.fields[key] = forms.ChoiceField(label=label, choices=options)
            self.initial.setdefault(key, actual)
        self._text, self._choice = text, choice
        if kind == "TEXT":
            text("text_value", self.spec.label, value.get("text", ""))
        elif kind == "COMPONENT":
            self.component("component", self.spec.label, value)
        elif kind == "CODED":
            choice("code", self.spec.label, [(c, CODE_LABELS[c]) for c in self.spec.codes], value.get("code", "UNKNOWN" if "UNKNOWN" in self.spec.codes else "NOT_STATED"))
            text("coded_raw", "类别或方向原词", value.get("raw", ""))
        elif kind == "VARIANT_IDENTITY":
            choice("kind", "原件变异类型", [("SMALL_VARIANT", "小变异"), ("COPY_NUMBER", "拷贝数变化"), ("FUSION", "融合")], value.get("kind", "SMALL_VARIANT"))
            choice("status", "身份组件核对状态", [("INCOMPLETE", "组件尚未完整"), ("COMPLETE", "必要组件已逐项核对")], value.get("status", "INCOMPLETE"))
            choice("scope", "原件注明的检测来源", [("UNKNOWN", "尚未判断"), ("SOMATIC", "体细胞"), ("GERMLINE", "胚系")], value.get("scope", "UNKNOWN"))
            for key, label in (("gene", "基因"), ("expression", "完整变异表达"), ("change", "拷贝数变化原词")):
                self.component(key, label, value.get(key))
            for key, label in (("coding", "编码位点"), ("protein", "蛋白位点"), ("codons", "密码子"), ("transcripts", "转录本及版本"), ("locations", "位置及参考版本")):
                self.component(key, label, value.get(key), multiple=True)
            choice("order_meaning", "融合两侧顺序的原文含义", [("AS_PRINTED_UNKNOWN", "只保留印刷顺序，方向尚未判断"), ("FIVE_TO_THREE", "原件明确为 5′ 到 3′")], value.get("order_meaning", "AS_PRINTED_UNKNOWN"))
            for i in range(2):
                partner = (value.get("partners", []) + [{}, {}])[i]
                for key, label in (("gene", "基因"), ("transcripts", "转录本及版本"), ("breakpoints", "断点")):
                    self.component(f"partner_{i+1}_{key}", f"融合第 {i+1} 侧{label}", partner.get(key), multiple=key != "gene")
            text("value_raw", "完整身份的原文", value.get("raw", ""))
        elif kind in {"ALLELE_FRACTION", "COPY_NUMBER", "MSI", "TMB", "PANEL_SIZE"}:
            choice("status", "数值读取状态", [("UNRESOLVED", "原数值尚未解析"), ("PARSED", "按原文录入数值")], value.get("status", "UNRESOLVED"))
            numbers = value.get("values", [])
            text("scalar_1", "原文数值或范围下限", numbers[0] if numbers else "", required=False, maximum=64)
            text("scalar_2", "原文范围上限（单值留空）", numbers[1] if len(numbers) == 2 else "", required=False, maximum=64)
            choice("comparator", "原文比较关系", [("", "尚未判断"), ("EQ", "单值"), ("LT", "小于"), ("LE", "小于等于"), ("GT", "大于"), ("GE", "大于等于"), ("RANGE", "范围")], value.get("comparator") or "")
            self.fields["comparator"].required = False
            choice("unit_state", "单位核对状态", STATES, value.get("unit_state", "UNKNOWN"))
            text("unit", "原单位（不能自动补充或换算）", value.get("unit"), required=False, maximum=128)
            self.fields["approximate"] = forms.BooleanField(label="原件有“约”等近似限定", required=False)
            self.initial.setdefault("approximate", value.get("approximate", False))
            choice("quantity_assertion", "数值原文限定", [("AS_REPORTED_NO_POSITIVITY_INFERRED", "只保留原值，不推断阳性"), ("UNCERTAIN", "原文明示数值不确定")], value.get("assertion", "AS_REPORTED_NO_POSITIVITY_INFERRED"))
            if kind == "PANEL_SIZE":
                self.component("count_object", "原文计数对象", value.get("count_object"))
            text("value_raw", "数值及限定的原文", value.get("raw", ""))
        elif kind == "DRUG_GROUP":
            text("names", "药物原名称（按原顺序，每行一个）", "\n".join(value.get("names", [])), multiple=True)
            choice("relation", "原文药物关系", [("UNKNOWN", "关系尚未判断"), ("SINGLE", "原文单药"), ("AND", "原文组合"), ("OR", "原文或关系"), ("ALTERNATIVE", "原文备选")], value.get("relation", "UNKNOWN"))
            text("value_raw", "药物组的原文", value.get("raw", ""))
        elif kind == "DRUG_LEVEL":
            self.component("grade", "报告证据等级", value.get("grade"))
            self.component("system", "原文等级体系及版本", value.get("system"))
            text("value_raw", "等级及体系原文", value.get("raw", ""))
        elif kind == "MOLECULAR_NEGATIVE":
            text("text_value", "限定检测范围的结果原文", value.get("text", ""))
            codes = ("NEGATIVE", "NOT_DETECTED", "UNCERTAIN", "NOT_TESTED", "NOT_PROVIDED")
            choice("assertion", "原文明示结果", [(c, CODE_LABELS[c]) for c in codes], value.get("assertion", "NOT_DETECTED"))
            scope = value.get("scope", {})
            choice("scope_state", "范围核对状态", [("UNKNOWN", "范围尚未判断"), ("EXPLICIT", "原文明示范围")], scope.get("state", "UNKNOWN"))
            text("scope_raw", "检测范围原文", scope.get("raw"), required=False, maximum=4096)
            kinds = scope.get("detection_kinds", [])
            self.fields["detection_count"] = forms.IntegerField(label="原文检测种类项数（最多 32 项）", min_value=0, max_value=32)
            self.initial.setdefault("detection_count", len(kinds))
            try:
                count = int(self.data.get(self.add_prefix("detection_count"), 0) if self.is_bound else self.initial["detection_count"])
            except (TypeError, ValueError):
                count = 0
            self.detection_count = min(max(count, 0), 32)
            for i in range(self.detection_count):
                item = kinds[i] if i < len(kinds) else {}
                choice(f"detection_{i}_code", f"第 {i+1} 项检测种类", [("OTHER", "其他原文种类"), ("SMALL_VARIANT", "小变异"), ("COPY_NUMBER", "拷贝数"), ("FUSION", "融合"), ("MSI", "MSI"), ("TMB", "TMB")], item.get("code", "OTHER"))
                text(f"detection_{i}_raw", f"第 {i+1} 项检测种类原词", item.get("raw"), maximum=4096)
            for key, label in (("targets", "范围内原目标"), ("limitations", "原文限制")):
                text("scope_" + key, label + "（每行一项）", "\n".join(scope.get(key, [])), required=False, multiple=True)

    def component(self, name, label, value=None, *, multiple=False):
        value = value or {}
        self._choice(name + "_state", label + "的原件状态", STATES, value.get("state", "UNKNOWN"))
        actual = "\n".join(value.get("values", [])) if multiple else value.get("raw")
        self._text(name + "_raw", label + ("（按原顺序每行一项，保留重复项）" if multiple else "原词"), actual, required=False, multiple=multiple)

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        def component(name, *, multiple=False):
            actual = data[name + "_raw"]
            return {"state": data[name + "_state"], "values": lines(actual)} if multiple else {"state": data[name + "_state"], "raw": actual or None}
        kind = self.spec.value_type
        if kind == "TEXT":
            value = {"text": data["text_value"]}
        elif kind == "COMPONENT":
            value = component("component")
        elif kind == "CODED":
            value = {"code": data["code"], "raw": data["coded_raw"]}
        elif kind == "VARIANT_IDENTITY":
            value = {k: data[k] for k in ("kind", "status", "scope")}
            value["raw"] = data["value_raw"]
            if value["kind"] == "FUSION":
                value.update(expression=component("expression"), order_meaning=data["order_meaning"],
                    partners=[{k: component(f"partner_{i}_{k}", multiple=k != "gene") for k in ("gene", "transcripts", "breakpoints")} for i in (1, 2)])
            else:
                value["gene"] = component("gene")
                if value["kind"] == "COPY_NUMBER":
                    value["change"] = component("change")
                else:
                    value["expression"] = component("expression")
                    value.update({k: component(k, multiple=True) for k in ("coding", "protein", "codons", "transcripts", "locations")})
        elif kind in {"ALLELE_FRACTION", "COPY_NUMBER", "MSI", "TMB", "PANEL_SIZE"}:
            if not data["scalar_1"] and data["scalar_2"]:
                raise forms.ValidationError("不能只有上限；请按原文连续填写。")
            value = {"status": data["status"], "values": [data[k] for k in ("scalar_1", "scalar_2") if data[k]],
                "comparator": data["comparator"] or None, "unit": data["unit"] or None, "unit_state": data["unit_state"],
                "approximate": data["approximate"], "measurement_kind": kind, "assertion": data["quantity_assertion"], "raw": data["value_raw"]}
            if kind == "PANEL_SIZE":
                value["count_object"] = component("count_object")
        elif kind == "DRUG_GROUP":
            value = {"names": lines(data["names"]), "relation": data["relation"], "raw": data["value_raw"]}
        elif kind == "DRUG_LEVEL":
            value = {"grade": component("grade"), "system": component("system"), "raw": data["value_raw"]}
        else:
            value = {"text": data["text_value"], "assertion": data["assertion"], "scope": {"state": data["scope_state"], "raw": data["scope_raw"] or None,
                "detection_kinds": [{"code": data[f"detection_{i}_code"], "raw": data[f"detection_{i}_raw"]} for i in range(self.detection_count)],
                "targets": lines(data["scope_targets"]), "limitations": lines(data["scope_limitations"])}}
        validate_value(self.field_key, value)
        data["value"] = value
        return data


class MolecularRevisionForm(MolecularValueForm):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
