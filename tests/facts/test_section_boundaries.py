from types import SimpleNamespace

from apps.facts.extraction import section_candidates


def blocks(texts):
    return [SimpleNamespace(text=text, document_page_id="page-1", reading_order=i, polygon=[])
            for i, text in enumerate(texts)]


def excerpts(items, document_type="UNKNOWN"):
    return [(row["category"], "\n".join(row["lines"])) for row in section_candidates(items, document_type)]


def test_lab_diagnosis_stops_before_unpunctuated_table_labels_and_ids():
    assert excerpts(blocks(["诊断：合成病名", "标本状态正常", "序代号", "项目名称", "结果", "42"]), "LAB") == [
        ("DIAGNOSIS", "诊断：合成病名")]
    assert excerpts(blocks(["诊断：合成病名", "12345678901", "项目名称", "结果"]), "LAB") == [
        ("DIAGNOSIS", "诊断：合成病名")]


def test_blank_diagnosis_does_not_consume_assay_description_or_slash_field():
    assert excerpts(blocks(["临床诊断：", "检测仪器：合成仪器", "检测概览", "合成正文"])) == []
    assert excerpts(blocks(["临床诊断：/", "床号：/", "样本性状：正常"])) == []
    assert excerpts(blocks(["临床诊断：", "本次检测概览", "送检目的", "合成检测项目",
                           "检测结果", "合成检测说明"])) == []


def test_surgery_does_not_adopt_discharge_date_and_narrative_diagnosis_stops_at_history():
    assert excerpts(blocks(["手术名称：合成手术", "出院日期：2026-08-11", "出院诊断：合成病名",
                           "入院时情况（主要症状）：后续治疗历史"])) == [
        ("TREATMENT", "手术名称：合成手术"), ("DIAGNOSIS", "出院诊断：合成病名")]


def test_imaging_page_headings_work_when_global_document_metadata_is_unknown_or_bundle():
    assert excerpts(blocks(["CT诊断报告书", "临床诊断：合成病名", "检查项目：胸部CT", "影像表现：所见",
                           "诊断意见：未见明确异常，考虑炎症可能。", "审核医师：合成甲"]), "LAB") == [
        ("DIAGNOSIS", "临床诊断：合成病名"), ("IMAGING", "诊断意见：未见明确异常，考虑炎症可能。")]


def test_adjacent_character_boxes_rejoin_without_merging_distant_columns():
    items = [
        SimpleNamespace(text=char, document_page_id="page-1", reading_order=i,
            polygon=[[.10+i*.01,.1],[.109+i*.01,.1],[.109+i*.01,.12],[.10+i*.01,.12]])
        for i,char in enumerate("诊断：合成病名")
    ]
    items.insert(1,SimpleNamespace(text="标本类型：血清", document_page_id="page-1", reading_order=50,
        polygon=[[.7,.1],[.9,.1],[.9,.12],[.7,.12]]))
    assert excerpts(items,"LAB") == [("DIAGNOSIS","诊断：合成病名")]


def test_clinical_history_is_literal_and_footer_never_becomes_diagnosis():
    assert excerpts(blocks(["现病史：2024年行合成手术，可能仍有病灶。", "既往史：另有记载",
                           "初步诊断：1.合成病名", "医师签名：合成医生", "合成医院"])) == [
        ("TREATMENT", "现病史：2024年行合成手术，可能仍有病灶。"), ("DIAGNOSIS", "初步诊断：1.合成病名")]


def test_page_end_is_explicit_and_cannot_join_next_page_without_a_heading():
    items=blocks(["初步诊断：1.合成病名"])
    items += [SimpleNamespace(text="2.另一合成病名",document_page_id="page-2",reading_order=0,polygon=[])]
    rows=section_candidates(items,"DISCHARGE")
    assert rows[0]["page_bounded"] is True
    assert len(rows)==1
    assert rows[0]["lines"]==["初步诊断：1.合成病名"]


def test_other_column_metadata_does_not_cut_wrapped_diagnosis():
    data=[("入院诊断：1.合成病名，",.1,.1,.48),("入院日期：2026-08-01",.6,.1,.9),
          ("2.另一合成病名。",.1,.14,.48),("手术名称：合成术",.1,.18,.48)]
    items=[SimpleNamespace(text=text,document_page_id="page-1",reading_order=i,
        polygon=[[x,y],[right,y],[right,y+.02],[x,y+.02]]) for i,(text,x,y,right) in enumerate(data)]
    assert excerpts(items,"DISCHARGE")[0] == ("DIAGNOSIS","入院诊断：1.合成病名，\n2.另一合成病名。")


def test_patient_provided_diagnosis_keeps_literal_qualification():
    note="注：以上受检者基本信息来自患者送检时提供信息，而非来自本次检测结果，本次检测不对此内容进行解读。"
    items=blocks(["病理诊断*：合成病名", "样本接收日期：2026-08-01", note, "样本质控结果"])
    assert excerpts(items,"PATHOLOGY") == [("DIAGNOSIS","病理诊断*：合成病名\n"+note)]


def test_trial_eligibility_tumor_type_column_is_not_patient_diagnosis():
    assert excerpts(blocks(["临床试验列表", "肿瘤类型", "实体瘤", "治疗药物/方案", "合成药甲", "Ⅰ期"]))==[]


def test_inline_receipt_metadata_is_not_appended_to_diagnosis():
    assert excerpts(blocks(["诊断：合成病名 接收时间：2026-08-01 09:10"]), "LAB") == [
        ("DIAGNOSIS", "诊断：合成病名")]


def test_reference_to_report_date_inside_imaging_conclusion_retains_context():
    text = "影像结论：请结合上次报告日期：2026-07-01 的检查作比较，尚不能确定。"
    assert excerpts(blocks([text]), "IMAGING") == [("IMAGING", text)]
