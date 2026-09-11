"""Explicit page transcriptions for the full ordered molecular association set."""
from django import forms

from .clinical_schema import FIELDS
from .molecular_forms import MolecularValueForm
from .molecular_schema import CONTEXT, TARGET_KEYS, ASSERTIONS, validate_context_shape, validate_assertion
from .pathology_forms import ROLE_LABELS as IHC_ROLE_LABELS, ASSERTION_LABELS

ROLE_LABELS = {**IHC_ROLE_LABELS, "REPORT_DRUG_EVIDENCE": "本报告的药物依据（不作为用药建议）"}
TARGET_LABELS = {"SPECIMEN": "标本", "ASSAY": "检测", "VARIANT": "完整变异身份", "DRUG_EVIDENCE": "药物依据组", "MARKER": "标记"}
UNKNOWN = [("unknown:NOT_STATED", "未找到明确关联"), ("unknown:AMBIGUOUS", "关联存在歧义"),
           ("unknown:SOURCE_INCOMPLETE", "来源不完整"), ("unknown:UNKNOWN", "归属尚未判断")]


class MolecularSourceForm(MolecularValueForm):
    page_number = forms.IntegerField(label="本字段原文所在页", min_value=1)
    expected_report_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    source_role = forms.ChoiceField(label="原文角色", choices=list(ROLE_LABELS.items()))

    def __init__(self, field_key, report, *args, anchors=(), bindings=(), fragments=(), association=None, reported_assertion=None, **kwargs):
        self.report = report
        self.targets = {str(a.pk): a for a in anchors}
        super().__init__(field_key, *args, **kwargs)
        self.slots = []
        for role in self.spec.roles:
            selected = [b for b in bindings if b["role"] == role]
            count = 8 if role == "VARIANT" and field_key.startswith("drug_evidence.") else 1
            choices = [(str(a.pk), f"第 {i+1} 个{TARGET_LABELS[role]}：{a.automatic_content['text']} · 第 {a.document_page.page_number} 页")
                       for i, a in enumerate(anchors) if a.field_key == TARGET_KEYS[role]]
            for index in range(count):
                slot = role.lower() + (f"_{index+1}" if count > 1 else "")
                self.slots.append((role, slot))
                label = TARGET_LABELS[role] + (f"（原顺序第 {index+1} 项）" if count > 1 else "")
                b = selected[index] if index < len(selected) else None
                self.fields["binding_" + slot] = forms.ChoiceField(label="关联的" + label,
                    choices=([("", "本项无额外目标")] if index else []) + UNKNOWN + choices, required=index == 0)
                self.initial["binding_" + slot] = (b["target_fact_id"] if b["state"] == "BOUND" else "unknown:" + b["reason"]) if b else ("unknown:NOT_STATED" if index == 0 else "")
                self.fields["proof_" + slot] = forms.CharField(label=label + "的完整身份与关联原文", required=False, max_length=30000, widget=forms.Textarea(attrs={"rows": 2}))
                self.fields["proof_page_" + slot] = forms.IntegerField(label=label + "依据所在页", required=False, min_value=1)
                proof = [fragments[i] for i in b["proof_fragment_ordinals"] if i < len(fragments)] if b else []
                same_page = len({p["page"] for p in proof}) == 1
                self.initial["proof_" + slot] = "\n".join(p["raw_text"] for p in proof) if same_page else ""
                self.initial["proof_page_" + slot] = proof[0]["page"] if same_page else ""
        if field_key.startswith("drug_evidence."):
            self.fields["association_raw"] = forms.CharField(label="证明整个药物与变异集合关系的原文", required=False, max_length=30000, widget=forms.Textarea(attrs={"rows": 3}))
            self.fields["association_page"] = forms.IntegerField(label="完整关联陈述所在页", required=False, min_value=1)
            self.initial["association_raw"] = (association or {}).get("raw") or ""
            proof = [fragments[i] for i in (association or {}).get("proof_fragment_ordinals", []) if i < len(fragments)]
            self.initial["association_page"] = proof[0]["page"] if len({p["page"] for p in proof}) == 1 else ""
        assertion = reported_assertion or {"code": "AS_REPORTED_NO_POSITIVITY_INFERRED", "raw": None}
        self.fields["reported_code"] = forms.ChoiceField(label="本字段原文明示的结果断言", choices=[(c, label) for c, label in ASSERTION_LABELS.items() if c in ASSERTIONS])
        self.fields["reported_raw"] = forms.CharField(label="明示断言的原词（必须在上方本字段原文中）", required=False, max_length=30000)
        self.initial.update(reported_code=assertion["code"], reported_raw=assertion["raw"] or "")
        self.fields["supplemental_count"] = forms.IntegerField(label="补充原文页数（字段或关联跨页时填写）", min_value=0, max_value=80, required=False,
            help_text="改变项数并提交后，可逐页填写对应原文。每段文字须属于所填页；不能将跨页依据归到第一页。")
        self.initial.setdefault("supplemental_count", 0)
        try:
            count = int(self.data.get(self.add_prefix("supplemental_count"), 0) or 0) if self.is_bound else self.initial["supplemental_count"]
        except (ValueError, TypeError):
            count = 0
        self.supplemental_count = min(max(count, 0), 80)
        for i in range(self.supplemental_count):
            self.fields[f"supplemental_{i}_page"] = forms.IntegerField(label=f"第 {i+1} 段补充原文所在页", min_value=1)
            self.fields[f"supplemental_{i}_text"] = forms.CharField(label=f"第 {i+1} 段补充原文（包含该页的完整限定）", max_length=30000, widget=forms.Textarea(attrs={"rows": 3}))

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        fragments = [{"page_number": data["page_number"], "raw_text": data["raw_value"]},
                     *[{"page_number": data[f"supplemental_{i}_page"], "raw_text": data[f"supplemental_{i}_text"]} for i in range(self.supplemental_count)]]
        supplemental = list(range(1, len(fragments)))
        def retain(page, text):
            item = {"page_number": page, "raw_text": text}
            if item not in fragments:
                fragments.append(item)
            return fragments.index(item)
        bindings, gap = [], False
        for role, slot in self.slots:
            identity = data["binding_" + slot]
            if not identity:
                if role == "VARIANT":
                    gap = True
                continue
            if role == "VARIANT" and gap:
                raise forms.ValidationError("变异关联须按原顺序连续填写，不能跳项。")
            if identity.startswith("unknown:"):
                binding = {"role": role, "state": "UNKNOWN", "target_fact_id": None, "target_entity_key": None,
                    "proof_fragment_ordinals": [], "reason": identity.split(":", 1)[1]}
            else:
                if not data["proof_" + slot] or not data["proof_page_" + slot]:
                    self.add_error("proof_" + slot, "明确关联须有该完整身份的原文和页码；没有依据请保留未关联。")
                    continue
                target = self.targets[identity]
                proof_index = retain(data["proof_page_" + slot], data["proof_" + slot])
                binding = {"role": role, "state": "BOUND", "target_fact_id": identity, "target_entity_key": target.entity_key,
                    "proof_fragment_ordinals": list(dict.fromkeys([proof_index, *supplemental])), "reason": None}
            bindings.append(binding)
        association = None
        if self.field_key.startswith("drug_evidence."):
            variants = [b for b in bindings if b["role"] == "VARIANT"]
            known = variants and all(b["state"] == "BOUND" for b in variants)
            association = {"state": "UNKNOWN", "raw": None, "proof_fragment_ordinals": []}
            if known:
                if not data["association_raw"] or not data["association_page"]:
                    self.add_error("association_raw", "须转录证明整个目标集合关系的原文及页码，不能只选择其中已知项。")
                else:
                    index = retain(data["association_page"], data["association_raw"])
                    association = {"state": "EXPLICIT", "raw": data["association_raw"], "proof_fragment_ordinals": list(dict.fromkeys([index, *[i for b in variants for i in b["proof_fragment_ordinals"]]]))}
            elif data["association_raw"] or data["association_page"]:
                self.add_error("association_raw", "关联尚未判断时不能补造确定的目标集合；原文可保留在本字段转录中。")
        assertion = {"code": data["reported_code"], "raw": data["reported_raw"] or None,
                     "proof_fragment_ordinals": [0, *supplemental] if data["reported_raw"] else []}
        validate_assertion(assertion)
        context = {"context_version": CONTEXT, "membership_policy": CONTEXT, "report_id": str(self.report.pk), "bindings": bindings, "association": association}
        if not self.errors:
            validate_context_shape(self.field_key, context)
        data.update(fragments=fragments, entity_context=context, reported_assertion=assertion, own_fragment_count=1+self.supplemental_count)
        return data
