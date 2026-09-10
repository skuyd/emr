"""Readable full components for authorized review; not an output projection."""


def details(content):
    value = content["value"]
    rows = []
    def add(label, text):
        rows.append({"label": label, "text": str(text)})
    def component(label, item):
        if item is None:
            return
        state = item.get("state")
        add(label, "\n".join(item["values"]) if state == "PRINTED" and "values" in item else item.get("raw") if state == "PRINTED" else
            {"UNKNOWN": "尚未判断", "NOT_PRINTED": "已核对原件未印"}.get(state, "尚未判断"))
    if content["field_key"] == "variant.identity":
        add("类型", {"SMALL_VARIANT": "小变异", "COPY_NUMBER": "拷贝数变化", "FUSION": "融合"}[value["kind"]])
        add("检测来源", {"SOMATIC": "体细胞", "GERMLINE": "胚系", "UNKNOWN": "尚未判断"}[value["scope"]])
        add("组件状态", "逐项完整" if value["status"] == "COMPLETE" else "尚未完整")
        for key, label in (("gene", "基因"), ("expression", "完整表达"), ("coding", "编码位点"), ("protein", "蛋白位点"), ("codons", "密码子"), ("transcripts", "转录本及版本"), ("locations", "位置及参考版本"), ("change", "拷贝数变化")):
            component(label, value.get(key))
        if value["kind"] == "FUSION":
            add("两侧顺序", "原件明确 5′ 到 3′" if value["order_meaning"] == "FIVE_TO_THREE" else "保留印刷顺序，方向尚未判断")
            for i, partner in enumerate(value["partners"], 1):
                for key, label in (("gene", "基因"), ("transcripts", "转录本及版本"), ("breakpoints", "断点")):
                    component(f"第 {i} 侧{label}", partner[key])
    elif content["field_key"] == "assay.negative_statement":
        scope = value["scope"]
        add("原文范围", scope["raw"] if scope["state"] == "EXPLICIT" else "范围尚未判断")
        for item in scope["detection_kinds"]:
            add("检测种类原词", item["raw"])
        for key, label in (("targets", "原目标"), ("limitations", "原限制")):
            for text in scope[key]:
                add(label, text)
    elif content["field_key"] == "drug_evidence.drugs":
        add("药物关系", {"SINGLE": "单药", "AND": "组合", "OR": "或", "ALTERNATIVE": "备选", "UNKNOWN": "尚未判断"}[value["relation"]])
        for i, name in enumerate(value["names"], 1):
            add(f"第 {i} 个原名称", name)
    elif content["field_key"] == "drug_evidence.level":
        component("原证据等级", value["grade"])
        component("原等级体系及版本", value["system"])
    assertion = content.get("reported_assertion")
    if assertion and assertion["raw"]:
        add("本字段原文明示断言", assertion["raw"])
    return rows
