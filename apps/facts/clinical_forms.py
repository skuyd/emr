from copy import deepcopy

from django import forms

from .clinical_schema import FIELDS, validate_value


class ReportForm(forms.Form):
    title = forms.CharField(label="报告名称", max_length=256)
    first_page = forms.IntegerField(label="原件起始页", min_value=1)
    last_page = forms.IntegerField(label="原件结束页", min_value=1)
    expected_lifecycle_revision = forms.IntegerField(widget=forms.HiddenInput)
    expected_version_id = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self):
        values = super().clean()
        if values.get("first_page", 0) > values.get("last_page", 0):
            raise forms.ValidationError("报告起始页不能晚于结束页。")
        if values.get("last_page", 0) - values.get("first_page", 0) > 999:
            raise forms.ValidationError("一次报告范围最多1000页。")
        return values


class ReportActionForm(forms.Form):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    expected_source = forms.CharField(widget=forms.HiddenInput, max_length=64)


class BoundaryReplacementForm(ReportActionForm):
    title = forms.CharField(label="更正后的报告名称", max_length=256)
    mode = forms.ChoiceField(label="报告范围", choices=[("pages", "按完整原件页"), ("ocr", "按起止原文选择")])
    first_page = forms.IntegerField(label="起始页（按完整页时必填）", required=False, min_value=1)
    last_page = forms.IntegerField(label="结束页（含）", required=False, min_value=1)
    first_ocr_block = forms.ChoiceField(label="起始原文", required=False)
    last_ocr_block = forms.ChoiceField(label="结束原文", required=False)
    start_text = forms.CharField(label="起始原文中从这段文字开始（留空为该段开头）", required=False, max_length=2000)
    end_text = forms.CharField(label="结束原文中包含这段文字为止（留空为该段末尾）", required=False, max_length=2000)

    def __init__(self, report, *args, **kwargs):
        self.report = report
        self.blocks = list(report.parsing_version.ocr_blocks.select_related("document_page").order_by("document_page__page_number", "reading_order", "pk")) if report.parsing_version_id else []
        super().__init__(*args, **kwargs)
        choices = [("", "请选择"), *[(str(block.pk), f"第{block.document_page.page_number}页 · {block.text[:180]}") for block in self.blocks]]
        self.fields["first_ocr_block"].choices = choices
        self.fields["last_ocr_block"].choices = choices

    def clean(self):
        values = super().clean()
        if self.errors:
            return values
        if values["mode"] == "pages":
            first, last = values["first_page"], values["last_page"]
            if not first or not last or not first <= last <= self.report.document.page_count or last - first >= 1000:
                raise forms.ValidationError("请明确选择本份原件中连续的起止页。")
            values["spans"] = [{"page_number": number} for number in range(first, last + 1)]
            return values
        ids = [str(block.pk) for block in self.blocks]
        if values["first_ocr_block"] not in ids or values["last_ocr_block"] not in ids:
            raise forms.ValidationError("请选择本份原件的起止原文。")
        first, last = ids.index(values["first_ocr_block"]), ids.index(values["last_ocr_block"])
        if first > last:
            raise forms.ValidationError("起始原文不能晚于结束原文。")
        spans = []
        for index in range(first, last + 1):
            block, start, end = self.blocks[index], 0, len(self.blocks[index].text)
            for key, active in (("start_text", index == first), ("end_text", index == last)):
                anchor = values[key]
                if active and anchor:
                    if block.text.count(anchor) != 1:
                        raise forms.ValidationError("起止文字必须在所选原文中准确出现一次；重复时请填写更完整的文字。")
                    if key == "start_text":
                        start = block.text.index(anchor)
                    else:
                        end = block.text.index(anchor) + len(anchor)
            if start >= end:
                raise forms.ValidationError("起止文字顺序无效。")
            spans.append({"page_number": block.document_page.page_number, "ocr_block_id": str(block.pk), "start_offset": start, "end_offset": end})
        values["spans"] = spans
        return values


class ClinicalValueForm(forms.Form):
    raw_value = forms.CharField(label="原件字段文字", max_length=30000, widget=forms.Textarea(attrs={"rows": 3}))
    checked_original = forms.BooleanField(label="我已对照原件核对字段值、部位、单位和限定表达", required=False)

    def __init__(self, field_key, *args, value=None, **kwargs):
        self.field_key = field_key
        self.spec = FIELDS[field_key]
        initial = dict(kwargs.pop("initial", {}) or {})
        value = value or {}
        self.scope_members = deepcopy(value.get('members', []))
        if self.spec.value_type == "TEXT":
            initial["text_value"] = value.get("text", "")
        elif self.spec.value_type == "DATE":
            initial.update(date_value=value.get("value") or "", precision=value.get("precision", "UNKNOWN"))
        elif self.spec.value_type == "CODED":
            initial["code"] = value.get("code", "")
            initial["coded_raw"] = value.get("raw", "")
        elif self.spec.value_type == "SCALAR":
            numbers = value.get("values", [])
            initial.update(scalar_1=numbers[0] if numbers else "", scalar_2=numbers[1] if len(numbers) == 2 else "",
                           comparator=value.get("comparator", "EQ"), original_unit=value.get("unit") or "",
                           approximate=value.get("approximate", False), measurement_role=value.get("measurement_role", "CURRENT"))
        elif self.spec.value_type == 'SCOPED_LATERALITY':
            for index, member in enumerate(self.scope_members, 1):
                initial.update({f'member_{index}_code': member['code'], f'member_{index}_raw': member['raw']})
        else:
            initial["approximate"] = value.get("approximate", False)
            initial["measurement_role"] = value.get("measurement_role", "CURRENT")
            for i, component in enumerate(value.get("components", []), 1):
                initial.update({f"size_{i}": component["value"], f"unit_{i}": component["unit"], f"axis_{i}": component["axis"] or ""})
        super().__init__(*args, initial=initial, **kwargs)
        if self.spec.value_type == "TEXT":
            self.fields["text_value"] = forms.CharField(label=self.spec.label, max_length=30000, widget=forms.Textarea(attrs={"rows": 4}))
        elif self.spec.value_type == "DATE":
            self.fields["date_value"] = forms.CharField(label="日期（按原件写至年、月或日）", required=False, max_length=10,
                                                        help_text="例如2026、2026-08或2026-08-17；时间不详时留空。")
            self.fields["precision"] = forms.ChoiceField(label="日期精度", choices=[("UNKNOWN", "时间不详"), ("YEAR", "年"), ("MONTH", "月"), ("DAY", "日")])
        elif self.spec.value_type == "CODED":
            labels = {"CT": "CT", "MR": "磁共振", "PET_CT": "PET/CT", "US": "超声", "XRAY": "X线",
                      "LEFT": "左", "RIGHT": "右", "BILATERAL": "双侧", "MIDLINE": "中线",
                      "GROUP_LARGER": "本段较大者（原文组内限定）", "REPORT_MAXIMUM": "原文明示全报告最大病灶"}
            self.fields["code"] = forms.ChoiceField(label=self.spec.label, choices=[(v, labels[v]) for v in self.spec.codes])
            self.fields["coded_raw"] = forms.CharField(label="对应类别的原文文字", max_length=512,
                                                        help_text="保留这项类别的原词；完整来源片段仍单独保留。")
        elif self.spec.value_type == "SCALAR":
            self.fields["scalar_1"] = forms.CharField(label="原文数值或范围下限", max_length=30)
            self.fields["scalar_2"] = forms.CharField(label="范围上限（只有原文写明范围时填写）", required=False, max_length=30)
            self.fields["comparator"] = forms.ChoiceField(label="原文数值关系", choices=[
                ("EQ", "单个数值"), ("LT", "小于"), ("LE", "小于或等于"), ("GT", "大于"), ("GE", "大于或等于"), ("RANGE", "范围"),
            ])
            self.fields["original_unit"] = forms.CharField(label="原单位", required=False, max_length=30,
                                                            help_text="原单位未注明时留空，不自动补单位。")
            self.fields["approximate"] = forms.BooleanField(label="原文包含“约”或类似近似限定", required=False)
            self.fields["measurement_role"] = forms.ChoiceField(label="该数值在原文中的时间", choices=[
                ("CURRENT", "本次检查"), ("HISTORICAL", "此前检查的引用值"), ("UNKNOWN", "时间角色不详"),
            ])
        elif self.spec.value_type == 'SCOPED_LATERALITY':
            choices = [('LEFT', '左'), ('RIGHT', '右'), ('BILATERAL', '双侧'), ('MIDLINE', '中线')]
            for index, member in enumerate(self.scope_members, 1):
                self.fields[f'member_{index}_code'] = forms.ChoiceField(label=f"{member['site_text']}的侧别（仅限此部位）", choices=choices)
                self.fields[f'member_{index}_raw'] = forms.CharField(label=f"{member['site_text']}的原文转录", max_length=512,
                    help_text='更换、增加或移除部位须使用范围替换，原位置和原区间不能在本表覆盖。')
        else:
            self.fields["approximate"] = forms.BooleanField(label="原文包含“约”或类似近似限定", required=False)
            self.fields["measurement_role"] = forms.ChoiceField(label="该尺寸在原文中的时间", choices=[("CURRENT", "本次检查"), ("HISTORICAL", "此前检查的引用值"), ("UNKNOWN", "时间角色不详")])
            axes = [("", "原文未注明轴"), ("LONG", "长径"), ("SHORT", "短径"), ("DIAMETER", "直径"),
                    ("WIDTH", "宽"), ("HEIGHT", "高"), ("DEPTH", "厚/深"), ("AP", "前后径"),
                    ("TRANSVERSE", "横径"), ("CRANIOCAUDAL", "头尾径")]
            for i in range(1, 4):
                self.fields[f"size_{i}"] = forms.CharField(label=f"第{i}个尺寸（无则留空）", required=i == 1, max_length=30)
                self.fields[f"unit_{i}"] = forms.ChoiceField(label=f"第{i}个尺寸原单位", choices=[("mm", "mm"), ("cm", "cm"), ("毫米", "毫米"), ("厘米", "厘米")], initial="mm")
                self.fields[f"axis_{i}"] = forms.ChoiceField(label=f"第{i}个尺寸的原文轴名称", choices=axes, required=False)

    def clean(self):
        values = super().clean()
        if self.errors:
            return values
        if self.spec.value_type == "TEXT":
            value = {"text": values["text_value"]}
        elif self.spec.value_type == "DATE":
            value = {"value": values["date_value"] or None, "precision": values["precision"]}
        elif self.spec.value_type == "CODED":
            value = {"code": values["code"], "raw": values["coded_raw"]}
        elif self.spec.value_type == "SCALAR":
            numbers = [values["scalar_1"]] + ([values["scalar_2"]] if values["scalar_2"] else [])
            value = dict(values=numbers, comparator=values["comparator"], unit=values["original_unit"] or None,
                         approximate=values["approximate"], measurement_role=values["measurement_role"], raw=values["raw_value"])
        elif self.spec.value_type == 'SCOPED_LATERALITY':
            members = [{**member, 'code': values[f'member_{index}_code'], 'raw': values[f'member_{index}_raw']}
                       for index, member in enumerate(self.scope_members, 1)]
            value = {'scope': 'NAMED_MEMBERS_ONLY', 'members': members}
        else:
            components = [{"value": values[f"size_{i}"], "unit": values[f"unit_{i}"], "axis": values[f"axis_{i}"] or None}
                          for i in range(1, 4) if values.get(f"size_{i}")]
            if not values.get("size_2") and values.get("size_3"):
                raise forms.ValidationError("请按原文顺序连续填写尺寸。")
            value = {"components": components, "approximate": values["approximate"], "measurement_role": values["measurement_role"], "raw": values["raw_value"]}
        validate_value(self.field_key, value)
        values["value"] = value
        return values


class ClinicalRevisionForm(ClinicalValueForm):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def __init__(self, *args, parent_context=None, **kwargs):
        super().__init__(*args, **kwargs)
        if parent_context:
            self.fields['expected_parent_revision'] = forms.IntegerField(min_value=0, required=parent_context['revision'] is not None,
                                                                         widget=forms.HiddenInput)
            self.fields['expected_parent_source'] = forms.CharField(max_length=64, widget=forms.HiddenInput)
            self.initial.update(expected_parent_revision=parent_context['revision'], expected_parent_source=parent_context['source'])


class ManualClinicalFieldForm(ClinicalValueForm):
    page_number = forms.IntegerField(label="字段来源页", min_value=1)
    expected_report_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    entity = forms.ChoiceField(label="本报告中的病灶", required=False)

    def __init__(self, field_key, *args, entities=(), **kwargs):
        super().__init__(field_key, *args, **kwargs)
        if FIELDS[field_key].entity_kind == "report":
            self.fields.pop("entity")
        else:
            comparison = FIELDS[field_key].entity_kind == "comparison"
            self.fields["entity"].label = "本报告中的对比原文" if comparison else "本报告中的病灶或局部异常"
            self.fields["entity"].choices = [("new", "新增一条对比原文" if comparison else "新增一处病灶或局部异常"), *entities]
            self.fields["entity"].required = True
