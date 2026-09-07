"""Frozen text shared by the HTML quick card and its measured PDF layout."""
from apps.treatments.forms import KINDS, OCCURRENCES


def _value(value):
    return str(value) if value is not None else "未具备可计算来源"


def card_sections(snapshot):
    included = {row["key"] for row in snapshot["card"]["sections"] if row["included"]}
    confirmed, pending = [], []
    explicitly_pending = snapshot["selection"].get("include_pending_cycles", False)

    def append(row, text):
        if row["status"] == "PENDING":
            if explicitly_pending:
                pending.append({"text": "候选，尚待确认：" + text})
        else:
            confirmed.append({"text": text})

    if "treatment" in included:
        for row in snapshot.get("treatment_events", []):
            content = row["content"]
            if not row["source_complete"]:
                append(row, "选定治疗未包含必要来源，相关内容已省略。")
                continue
            origin = "本人补记" if row["origin"] == "USER" else "原文来源"
            kind = dict(KINDS).get(content["kind"], "治疗记录")
            occurred = dict(OCCURRENCES).get(content["occurrence"], "发生状态不明")
            append(row, f'{content["title"]}；{kind}；{content["date"] or "日期不明"}（{content["date_precision"]}）；'
                        f'{occurred}；{origin}；{content.get("note") or ""}；记录 {row["id"]}，修订 {row["revision_number"]}。')
        for row in snapshot.get("treatment_regimens", []):
            append(row, f'方案：{row["content"]["text"] or "未纳入必要来源，内容已省略"}；修订 {row["revision_number"]}。')
        for row in snapshot.get("treatment_cycles", []):
            content = row["content"]
            number = f'C{content["ordinal"]}' if content["ordinal"] else "序号不明"
            append(row, f'组织锚点：{content["anchor"] or "未知或未纳入必要来源"}；{number}；'
                        f'实际治疗结束：{content["end"] or "不明"}；展示组织不表示医学周期起止；周期 {row["id"]}，修订 {row["revision_number"]}。')
        if "labs" in included:
            for row in snapshot.get("cycle_points", []):
                text = (f'{row["standard_code"]}：{row["date"]}，距锚点 {row["relative_day"]} 天，{row["value"]} {row["unit"]}；'
                        f'{row["label_text"] or "完整明细点"}；检验来源 {row["observation_id"]}。')
                append({"status": "PENDING" if row["preview"] else "CONFIRMED"}, text)
    result = []
    if confirmed:
        result.append({"key": "treatment_derived", "title": "选定治疗与周期", "included": True, "entries": confirmed})
    if "labs" in included and snapshot.get("personal_changes"):
        entries = []
        for row in snapshot["personal_changes"]:
            entries.append({"text": f'{row["label"] or "检验记录"}；{row["date"] or "日期未纳入"}；当前 {_value(row["current_value"])} {row["unit"] or ""}；'
                f'距前次 {_value(row["elapsed_days"])} 天，绝对变化 {_value(row["absolute_change"])}, 每日变化 {_value(row["daily_change"])}；'
                f'相对前次 {_value(row["previous_percentage"])}%；近三次均值 {_value(row["baseline_mean"])}，相对均值 {_value(row["baseline_percentage"])}%；'
                f'本次提示阈值 {row["threshold_percent"]}%；缺少已选来源时相关值保持缺失，不替换原基线；记录 {row["id"]}。'})
        result.append({"key": "personal_changes", "title": "选定个人变化", "included": True, "entries": entries})
    if pending:
        result.append({"key": "treatment_candidates", "title": "尚待确认的候选内容（附页）", "included": True,
                       "appendix_only": True, "entries": pending})
    return result
