"""Human-readable relation limits. Do not turn a proposal into a diagnosis."""

from copy import deepcopy


STATUS_LABELS = {"UNASSIGNED": "未关联", "PENDING": "待核对", "CONFIRMED": "已确认关联", "REJECTED": "已拒绝",
                 "DEFERRED": "暂缓", "STALE": "来源或关联已变化，需重新核对", "UNAVAILABLE": "来源不可用",
                 "NOT_PROPOSED": "已撤回提议", "RULE_OUTDATED": "提议规则已更新，需重新生成"}
REASON_LABELS = {
    "explicit_location_equal": "原文部位相同（比较时忽略空白及全半角差异）",
    "explicit_side_equal": "原文侧别相同", "explicit_body_equal": "检查部位相同",
    "reference_date_equal": "原文引用日期与这份检查日期相同；仍需核对是否为同一次检查",
}
LIMIT_LABELS = {
    "multiple_candidates": "同一报告还有其他相似候选，请逐一核对，不能按尺寸接近程度自动选择。",
    "ambiguous_location": "部位有多个原文值，尚不能确定唯一位置。",
    "side_unknown_or_ambiguous": "侧别缺少明确、唯一的原文值。",
    "side_scope_not_whole": "侧别仅限组内列明部位或原范围未记录，不能用于整体同侧或异侧判断。",
    "proposal_rule_outdated": "旧提议的侧别范围规则已更新，原依据仅作历史记录；新确认须重新生成提议。",
    "body_unknown_or_ambiguous": "检查部位缺少明确、唯一的原文值。",
    "method_unknown": "检查方法不详。", "method_changed": "检查方法不同，测量不直接相连。",
    "unconfirmed_source": "部分依据尚未核对；请先核对字段，再重新生成当前来源的提议。",
    "field_conflict": "来源字段存在冲突，冲突值都保留供核对。",
    "date_unreliable": "检查日期不详或精度不足，不补写日期。",
    "same_exam_day": "检查日期相同，不能据此确定先后或同一次检查。",
    "uncertain_comparison": "对比原文含不确定表达，请保留该限定。",
    "axis_unknown": "测量轴未明确，保留有序数值但不连线。",
    "axis_changed": "测量轴不同。", "unit_unrecognized": "原单位尚不能统一。",
    "unit_changed": "量纲或原单位不同。", "date_not_later": "日期不能确定为后一次检查。",
    "same_report": "同一报告的数值不能作为两次检查相减。",
    "historical_or_unknown_role": "历史值或时间角色不详，不重复记为本次测量。",
    "bounded_value": "原文是范围或比较值，不取中点。", "value_unavailable": "没有可用的明确数值。",
    "unassigned_observation": "观察尚未建立有效关联。", "lesion_changed": "属于不同病灶标识。",
    "measurement_kind_changed": "尺寸与 SUV 使用不同量纲。",
    "ambiguous_measurement": "同日有多个同轴测量，未选择其中一个来连接下一次检查。",
    "unavailable_observation": "这一标识还有来源失效的观察，保留当前有效点并暂停连线。",
}
ACTION_LABELS = {"PROPOSE": "生成关联提议", "MATCH": "确认关联", "REASSIGN": "改派关联", "CREATE": "建立观察标识",
                 "RENAME": "修改名称", "SPLIT": "拆分观察", "UNLINK": "取消关联", "UNDO": "撤销操作",
                 "REJECT": "拒绝提议", "DEFER": "暂缓提议"}


def display_observation(row):
    result = deepcopy(row)
    result["status_label"] = STATUS_LABELS[row["status"]]
    site = next((field for field in row["fields"] if field["field_key"] == "lesion.site" and field["source_valid"]), None)
    result["source_url"] = site["source"]["url"] if site and row["status"] != "UNAVAILABLE" else None
    result["label"] = " · ".join([row["lesion_name"] or " / ".join(row["site"]) or "来源观察",
                                   (row["date"] or {}).get("value") or "日期不详", row["report_title"]])
    return result


def display_proposal(row):
    return {**row, "status_label": STATUS_LABELS[row["status"]],
            "original_reason_labels": [REASON_LABELS.get(reason["code"], "原文依据需核对") for reason in row["original_reasons"]],
            "reason_labels": [REASON_LABELS.get(reason["code"], "原文依据需核对") for reason in row["reasons"]],
            "limit_labels": [LIMIT_LABELS.get(code, "存在尚未核对的限制。") for code in row["blockers"]]}


def display_operation(operation):
    effects = [(effect.sequence, effect.lesion.revision_number) for effect in operation.lesion_revisions.select_related("lesion")]
    effects += [(effect.sequence, effect.observation.revision_number) for effect in operation.observation_revisions.select_related("observation")]
    effects += [(effect.sequence, effect.proposal.revision_number) for effect in operation.proposal_revisions.select_related("proposal")]
    return {"id": str(operation.pk), "action": ACTION_LABELS.get(operation.action, "关联核对"),
            "author_id": operation.author_id, "created_at": operation.created_at,
            "checked_original": operation.checked_original, "reverses_id": operation.reverses_id,
            "may_undo": bool(effects) and all(recorded == current for recorded, current in effects)}


def display_trends(result):
    from .comparison import _point_blockers
    from .trends import AXIS_LABELS

    measurements = []
    for point in result["measurements"]:
        measurements.append({"point": point,
            "axis_label": "SUVmax" if point.kind == "SUV" else AXIS_LABELS.get(point.axis, f"第 {point.component_index + 1} 个尺寸（轴未明确）"),
            "role_label": {"CURRENT": "本次测量", "HISTORICAL": "原文引用历史值", "UNKNOWN": "时间角色不详"}[point.role],
            "limit_labels": [LIMIT_LABELS.get(code, "存在未核对限制。") for code in _point_blockers(point)]})
    return {**result, "measurement_rows": measurements,
            "comparisons": [{**row, "limit_labels": [LIMIT_LABELS.get(code, "存在未核对限制。") for code in row["reasons"]]}
                            for row in result["comparisons"]],
            "limit_labels": [LIMIT_LABELS.get(code, "存在未核对限制。") for code in result["limitations"]]}
