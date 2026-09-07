"""Plain-language presentation of review state and existing quality rules."""

from .models import RevisionAction


CATEGORY_LABELS = {
    'BIOCHEMISTRY': '生化', 'CARDIAC_MARKER': '心肌标志物', 'CBC': '血常规',
    'HEMATOLOGY': '血常规', 'COAGULATION': '凝血', 'ELECTROLYTES': '电解质',
    'ENDOCRINOLOGY': '内分泌', 'GLUCOSE_METABOLISM': '血糖代谢', 'IMMUNOLOGY': '免疫',
    'INFLAMMATION': '炎症相关', 'IRON_METABOLISM': '铁代谢', 'SERUM_IRON': '血清铁',
    'LIPIDS': '血脂', 'LIVER_FUNCTION': '肝功能', 'MOLECULAR_GENE': '分子检测',
    'NUTRITION': '营养相关', 'OTHER': '其他', 'PANCREATIC_ENZYME': '胰酶',
    'RENAL_FUNCTION': '肾功能', 'SEROLOGY': '血清学', 'STOOL': '粪便检查',
    'THYROID_FUNCTION': '甲状腺功能', 'TUMOR_MARKER': '肿瘤标志物',
    'URINALYSIS': '尿液检查', 'VIRAL_SCREEN': '病毒筛查',
}


ISSUE_DESCRIPTIONS = {
    "association_conflict": "项目、结果、单位等字段的对应关系可能有误，请对照原报告的同一行核实。",
    "recognition_uncertain": "部分文字或数字的识别把握不足，请放大原件核对。",
    "specimen_unknown": "尚未找到足够依据确定血液、尿液等标本类型，因此暂不能用于趋势比较。",
    "unit_unknown": "单位缺失或无法识别，暂不能可靠比较数值；原件有单位时可主动更正。",
    "source_unavailable": "当前结果的原件或字段来源无法读取，请稍后重试。",
    "reference_unknown": "报告未提供可识别的参考范围，暂不能对照范围。",
    "reference_conflict": "参考范围内容存在矛盾，请对照原件核实。",
    "date_uncertain": "尚未找到可靠的检查日期，请对照原件补充或更正。",
    "date_conflict": "识别到的日期不一致，请核实本项对应的检查日期。",
    "type_conflict": "结果内容与识别出的结果类型不一致，请核对数字、比较符或文字。",
    "mapping_unknown": "尚未确定本项对应的标准检验项目，请核对项目名称。",
    "magnitude_suspect": "数值满足疑似转录错误的检查条件，请特别核对小数点、数字和单位。",
    "internal_conflict": "同一报告中的相关结果存在不一致，请对照原件核实。",
    "normalization_uncertain": "系统曾整理或修复识别出的文字，请核对是否保留了原意。",
    "reported_error": "已记录识别有误；可填写正确内容，或在重新核对后确认与原件一致。",
    "revision_conflict": "重新识别的内容与此前人工修订不一致，请选择要采用的内容。",
    "specimen_conflict": "识别出的标本类型与标准项目不一致，请核对项目和标本。",
    "source_policy_unknown": "这份结果缺少当前质量检查依据，可从资料操作重新整理。",
    "numeric_unsupported": "该数值暂不能参与计算，仍保留原文供核对。",
}


REVIEW_STATES = {
    "AUTOMATIC": ("待核对", "neutral", "当前展示自动整理结果，尚未记录人工核对。"),
    RevisionAction.CONFIRM: ("已核对", "success", "已记录当前内容与原件一致，数值保持不变。"),
    RevisionAction.REPORT_ERROR: ("已标记识别有误", "warning", "已记录问题，尚未填写更正内容；该标记会限制结果参与比较。"),
    RevisionAction.DEFER: ("暂不处理", "neutral", "已记录暂缓处理，保留当前内容与已有提示，可稍后继续核对。"),
    RevisionAction.CORRECT: ("已更正", "success", "当前展示更正后的内容，原识别结果与修订历史仍保留。"),
    RevisionAction.KEEP_REVISION: ("已核对，沿用修订", "success", "已处理版本冲突，继续采用此前人工修订。"),
    RevisionAction.USE_AUTOMATIC: ("已核对，采用本次识别", "success", "已处理版本冲突，采用本次自动识别内容。"),
}


REVISION_FEEDBACK = {
    RevisionAction.CONFIRM: "已保存核对结果：当前内容与原件一致，数值保持不变。",
    RevisionAction.REPORT_ERROR: "已标记识别有误，可在主动更正中填写正确内容。",
    RevisionAction.DEFER: "已记录暂不处理，可稍后继续核对。",
    RevisionAction.CORRECT: "已保存更正，当前展示内容已更新。",
    RevisionAction.UNDO: "已撤销上次操作，当前内容与核对状态已恢复。",
    RevisionAction.KEEP_REVISION: "已保存选择，继续采用此前人工修订。",
    RevisionAction.USE_AUTOMATIC: "已保存选择，已采用本次识别内容。",
}


def review_status(observation):
    if observation.revision_conflict:
        return {"label": "待核对版本冲突", "tone": "warning",
                "description": "本次识别与此前人工修订不一致，确认内容后还需选择采用哪个版本。"}
    label, tone, description = REVIEW_STATES.get(observation.review_state, REVIEW_STATES["AUTOMATIC"])
    if observation.reported_error and observation.review_state == RevisionAction.DEFER:
        description += " 此前的识别有误标记仍保留。"
    return {"label": label, "tone": tone, "description": description}


def explain_issues(issues):
    return tuple({**item, "description": ISSUE_DESCRIPTIONS.get(item["code"], "请对照原件核实本项内容。")}
                 for item in issues)


def summarize_issues(observations):
    grouped = {}
    for observation in observations:
        for item in {issue["code"]: issue for issue in observation.display_issues}.values():
            group = grouped.setdefault(item["code"], {"code": item["code"], "label": item["label"],
                "description": ISSUE_DESCRIPTIONS.get(item["code"], "请对照原件核实本项内容。"), "count": 0})
            group["count"] += 1
    return tuple(grouped.values())
